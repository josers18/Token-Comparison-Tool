# Heroku Port — Design Spec

**Status:** approved (brainstorm phase)
**Author:** josers18 + Claude
**Date:** 2026-05-07
**Heroku app:** `token-comparison-tool` (us region) — https://token-comparison-tool-cb60c8f1dcc3.herokuapp.com/

## 1. Problem statement

The Token Comparison Tool today runs on a developer laptop and benchmarks
"Claude Code calling Salesforce via local `sf` CLI" (Native) versus
"Claude Code calling Salesforce via the hosted MCP gateway" (MCP). The
core engine shells out to the `claude` CLI binary and uses the local
`sf` CLI for the Native tool path.

Neither binary exists on a Heroku dyno. We need a Heroku-native version
that preserves the **Native vs MCP** comparison axis, uses the three
**Heroku Managed Inference** addons already provisioned on the app
(claude-4-5-haiku, claude-4-5-sonnet, claude-opus-4-5), persists state
across dyno restarts, and supports the Salesforce OAuth flow with a
Heroku callback URL.

## 2. Goals & non-goals

**Goals**
- Preserve the original "Native vs MCP" experiment exactly. Same
  scenarios, same prompt, same fairness guarantees (path order
  randomized per scenario; identical model/max-turns/org).
- Use the three Heroku Inference addons as a model dropdown, not as the
  comparison axis.
- Persist reports and per-session SF tokens in Postgres.
- Single-user OAuth via the existing ECA, but with `https://...herokuapp.com/callback`
  as the redirect URI.
- Run benchmarks in-process on the web dyno using FastAPI background
  tasks + SSE with heartbeats. No worker dyno, no Redis.
- Existing analysis, report writer, reverse parser, recommendations,
  and SPA rendering code must work unchanged. Only the runner is
  rewritten.

**Non-goals**
- Multi-tenant or per-user ECAs.
- Three-model shootout mode (haiku vs sonnet vs opus side-by-side).
- A worker dyno or Redis-backed queue.
- Migrating away from Pydantic / FastAPI / vanilla-JS SPA.
- Production-grade observability (no Datadog, no Sentry — heroku logs
  are sufficient at this scale).

## 3. Architecture overview

```
                       ┌────────────────────────────────────────────┐
                       │  Heroku app: token-comparison-tool         │
                       │  Dyno: web (uvicorn token_compare.api:app) │
                       └────────────────────────────────────────────┘
                            │
   browser (single user) ───┤  HTTPS
                            │
                            ▼
             ┌──────────────────────────────┐
             │  FastAPI app                 │
             │   ├─ static/ (SPA)           │
             │   ├─ /api/* endpoints        │
             │   ├─ /callback (OAuth)       │
             │   └─ SSE w/ heartbeats       │
             └──────────────────────────────┘
                  │              │             │
                  ▼              ▼             ▼
        ┌───────────────┐  ┌───────────┐  ┌────────────────────┐
        │ Heroku        │  │ Postgres  │  │ Salesforce org     │
        │ Inference x3  │  │ essential │  │  - REST API        │
        │  • haiku      │  │  -0       │  │    (Native path)   │
        │  • sonnet     │  │ sessions, │  │  - Hosted MCP gw   │
        │  • opus       │  │ reports,  │  │    (MCP path)      │
        │ + MCP conn    │  │ runs,     │  │                    │
        │               │  │ audit     │  │                    │
        └───────────────┘  └───────────┘  └────────────────────┘
```

### What's different vs. the local tool

- `runner.py` (subprocess to `claude -p`) → `messages_runner.py`
  (Anthropic Messages API client pointing at Heroku Inference).
- "Native" path = a small Python tool set that hits the org's REST
  API directly (`/services/data/vXX.X/query`, `/sobjects/{name}/describe`,
  Data Cloud query API).
- "MCP" path = the same Messages API call, but with `mcp_servers`
  pointed at the Salesforce Platform MCP gateway URLs the local tool
  was already hitting.
- OAuth callback: `https://token-comparison-tool-cb60c8f1dcc3.herokuapp.com/callback`.
  ECA's callback list must be updated to include this URL.
- SF tokens move from `.cache/sf-token.json` (filesystem, ephemeral on
  Heroku) into the `sessions` table, keyed by an HTTP-only signed cookie.
- Reports move from `reports/*.md` into the `reports` table. Markdown
  export is generated on demand by the existing `report.py` writer.
- One Procfile entry: `web: uvicorn token_compare.api:app --host 0.0.0.0 --port $PORT`.

### Heroku addons

All three Inference addons are already attached to the app:

| Addon | Attachment | Model |
|---|---|---|
| `heroku-inference:claude-4-5-haiku` | `HEROKU_INFERENCE_TEAL_*` | claude-4-5-haiku |
| `heroku-inference:claude-4-5-sonnet` | `INFERENCE_*` | claude-4-5-sonnet |
| `heroku-inference:claude-opus-4-5` | `HEROKU_INFERENCE_COBALT_*` | claude-opus-4-5 |

**To provision during the port:** `heroku-postgresql:essential-0`
attached as `DATABASE_URL`.

## 4. Components

| Module | Status | Responsibility |
|---|---|---|
| `src/token_compare/api.py` | Modified | Existing endpoints stay. SSE adds 15s heartbeat comments. Routes that read/write reports go through `db.py`. New `/api/models` endpoint exposing the three Inference model IDs. |
| `src/token_compare/messages_runner.py` | NEW (replaces `runner.py`) | Drives the Anthropic Messages API tool-use loop against Heroku Inference. Returns the same `RunResult` shape so analysis/report code is untouched. Aggregates `usage` across turns. Computes `total_cost_usd` from a static price table (Inference doesn't return cost). |
| `src/token_compare/native_tools.py` | NEW | Implements the Native-path tool set as Python functions registered in the Messages API `tools=[...]` list: `execute_soql(query)`, `describe_object(name)`, `list_sobjects(filter)`, `run_dc_query(sql)`, `list_dmos(filter)`. All hit `instance_url` REST endpoints with the session bearer token. |
| `src/token_compare/mcp_path.py` | NEW | Builds the `mcp_servers` parameter for the Messages call on the MCP path. Reads URLs from `config/sf-mcp.json` as a template; injects bearer at request time. |
| `src/token_compare/sf_auth.py` | Modified | Drop the localhost-only redirect guard; allow `https://*.herokuapp.com/callback`. Replace `.cache/sf-token.json` with `db.put_session_token` / `db.get_session_token`. Refresh + PKCE flow unchanged. |
| `src/token_compare/db.py` | NEW | Thin asyncpg wrapper. Tables: `sessions`, `reports`, `runs`, `inference_audit`. Idempotent migration runs at app startup. |
| `src/token_compare/sessions.py` | NEW | HTTP-only signed-cookie session middleware. Cookie holds an opaque ID; server looks up SF token from Postgres. Signed with `SESSION_SECRET` config var. |
| `src/token_compare/benchmark.py` | Modified | `run_benchmark(...)` calls `messages_runner.run_once`. Token lookup goes through `sessions.get_sf_token(session_id)`. |
| `src/token_compare/report.py`, `analysis.py`, `report_loader.py`, `recommendations.py`, `models.py` | Unchanged | Operate on `RunResult` / `BenchmarkResult` Pydantic types — same shapes, no edits. |
| `src/token_compare/preflight.py` | Modified | Replace "claude CLI installed?" / "sf CLI installed?" with: Inference env vars present, Postgres reachable, ECA env vars present, SF session active. |
| `static/app.js` | Minor | Add a model dropdown populated from `/api/models`. Drop "open localhost" copy. |
| `runner.py` | Removed | Replaced by `messages_runner.py`. |
| `mcp_config.py` | Removed | Was templating `${SF_ACCESS_TOKEN}` into a temp file for the `claude` CLI. New code injects the token directly into `mcp_servers` headers. |
| `config/sf-mcp.json` | Kept | Canonical upstream URL list, read at startup. |
| `Procfile`, `runtime.txt`, `app.json` | NEW | Standard Heroku Python deployment trio. `app.json` declares the addons so the app is one-click forkable. |
| `tests/` | Modified | Existing analysis/report tests stay. Subprocess runner tests are replaced by Anthropic-SDK-mocked runner tests. |

### The seam that makes this port clean

The codebase has a strict boundary between **runner** (produces
`RunResult`) and **everything downstream** (consumes `RunResult`).
~600 lines of analysis, reporting, reverse-parser, recommendations,
and SPA chart code never knew the data came from a subprocess. As
long as `messages_runner.py` returns the same Pydantic `RunResult`,
none of that code budges.

## 5. Data flow

### 5.1 Login flow (one-time per session)

```
Browser ──POST /api/sf/login──▶ FastAPI
                                 │
                                 ├─ generate state + PKCE verifier
                                 ├─ INSERT into sessions(id, ...)
                                 ├─ Set-Cookie: sid=<signed>
                                 └─ return { authorize_url }
Browser ──redirect to authorize_url──▶ Salesforce login.salesforce.com
SF ──redirect with ?code=──▶ /callback on Heroku app
                                 │
                                 ├─ exchange code → access_token
                                 ├─ UPDATE sessions SET sf_token_json=...
                                 └─ render "you can close this tab"
```

### 5.2 Benchmark run flow

```
Browser ──POST /api/run─────────▶ FastAPI
                                 │  reads sid cookie → sf_token from db
                                 ├─ INSERT reports(id, started_at, model)
                                 ├─ start asyncio Task with run_benchmark(...)
                                 └─ open SSE stream

For each scenario × path × run:
  benchmark.py ──▶ messages_runner.run_once(scenario, path, model, sf_token)

    Native path:
      anthropic.messages.create(
        model=$INFERENCE_MODEL_ID,
        tools=[execute_soql, describe_object, ...],
        messages=[user prompt])
      loop while stop_reason == "tool_use":
        for each tool_use block:
          call native_tools.<name>(args, sf_token)
        append tool_result, call messages.create again

    MCP path:
      anthropic.messages.create(
        model=$INFERENCE_MODEL_ID,
        mcp_servers=[salesforce_crm, data_cloud_queries
                     with Authorization: Bearer <sf_token>],
        messages=[user prompt])
      (Inference handles the MCP tool-use loop server-side)

    aggregate usage across turns → RunResult

  Each RunResult ──▶ INSERT runs(...)
                ──▶ queue.put_nowait(progress_event)
                ──▶ SSE: data: {...}\n\n

When all scenarios done:
  UPDATE reports SET payload_json=<full BenchmarkResult>
  SSE: data: { kind: "report_written", report_id }
```

**Heartbeats** — every 15s while SSE is alive: `: keepalive\n\n`
comments. Heroku's router idle timeout is 55s.

### 5.3 Report load flow

```
GET /api/reports                ──▶ SELECT FROM reports ORDER BY started_at DESC LIMIT 10
GET /api/reports/{id}/data      ──▶ SELECT payload_json hydrate _current_run cache
POST /api/reports/load (upload) ──▶ existing report_loader.py, no DB write
```

## 6. Schema

```sql
CREATE TABLE IF NOT EXISTS sessions (
  id            TEXT PRIMARY KEY,                 -- random 32 bytes hex
  sf_token_json JSONB,                            -- AccessToken pydantic dump; NULL until login completes
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at    TIMESTAMPTZ                       -- session cookie expiry; rolled on activity
);

CREATE TABLE IF NOT EXISTS reports (
  id           TEXT PRIMARY KEY,                  -- 'rpt_<ulid>'
  started_at   TIMESTAMPTZ NOT NULL,
  finished_at  TIMESTAMPTZ,
  model        TEXT NOT NULL,
  org_name     TEXT,
  operator     TEXT,
  payload_json JSONB,                             -- full BenchmarkResult; NULL while running
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS reports_started_at_idx ON reports (started_at DESC);

CREATE TABLE IF NOT EXISTS runs (
  id           TEXT PRIMARY KEY,                  -- 'run_<ulid>'
  report_id    TEXT REFERENCES reports(id) ON DELETE CASCADE,
  scenario_id  TEXT NOT NULL,
  path         TEXT NOT NULL,                     -- 'native' | 'mcp'
  run_index    INT NOT NULL,
  result_json  JSONB NOT NULL,                    -- RunResult pydantic dump
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS runs_report_id_idx ON runs (report_id);

CREATE TABLE IF NOT EXISTS inference_audit (
  id            BIGSERIAL PRIMARY KEY,
  run_id        TEXT REFERENCES runs(id) ON DELETE CASCADE,
  scenario_id   TEXT NOT NULL,
  path          TEXT NOT NULL,
  model         TEXT NOT NULL,
  prompt_hash   TEXT NOT NULL,                    -- sha256 of the prompt
  token_usage_json JSONB NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

A daily prune (cron task on dyno start, idempotent) deletes
`inference_audit` rows older than 30 days and `reports` beyond the 50
most recent (the SPA shows the 10 most recent — extra headroom is
cheap on essential-0).

## 7. Configuration

Runtime config — Heroku config vars:

| Var | Source | Notes |
|---|---|---|
| `INFERENCE_URL`, `INFERENCE_KEY`, `INFERENCE_MODEL_ID` | Heroku Inference (sonnet) addon | Default model. |
| `HEROKU_INFERENCE_TEAL_*` | Inference (haiku) addon | Cheapest model option. |
| `HEROKU_INFERENCE_COBALT_*` | Inference (opus) addon | Most capable. |
| `DATABASE_URL` | Heroku Postgres addon | Standard. |
| `SF_CLIENT_ID`, `SF_CLIENT_SECRET`, `SF_LOGIN_URL` | Manual | ECA credentials. |
| `SF_REDIRECT_URI` | Manual | `https://token-comparison-tool-cb60c8f1dcc3.herokuapp.com/callback`. |
| `SESSION_SECRET` | Manual (`heroku config:set`) | 32-byte hex; signs the session cookie. |
| `LOG_LEVEL` | Manual | Defaults to `INFO`. |

App startup refuses to boot if any of `DATABASE_URL`,
`SF_CLIENT_ID`, `SF_CLIENT_SECRET`, `SF_LOGIN_URL`, `SESSION_SECRET`,
`INFERENCE_URL`, `INFERENCE_KEY` are missing.

## 8. Error handling

| Failure | Detection | Behavior |
|---|---|---|
| Inference 5xx / network blip | `anthropic.APIError` | One retry, 1s backoff. Then fail that single run with `error="inference: <msg>"`; benchmark continues. |
| Inference 429 | `anthropic.RateLimitError` | Honor `Retry-After`, retry once. Then fail that run. |
| Tool-use loop hits `max_turns` | Counter in runner | Clean exit; `error="terminal_reason=max_turns"`, succeeded=false; tokens accumulated up to that point are recorded. |
| Native tool 401 from org | `httpx` response status | One refresh via `sf_auth.fetch_access_token`, retry the call once. If still 401, surface "sf auth expired — re-login" via SSE. |
| MCP gateway rejects token | Inference returns tool error | Same as 401 above. |
| Postgres connection drops | asyncpg exception | Pool reconnects. In-memory `_current_run` cache stays the source of truth so the SSE stream finishes; final report write retried once. |
| Benchmark exceeds 30 min process timeout | Heroku kills dyno | Active run dies. Partial `runs` rows stay queryable. Defaults are tuned so a typical run is under 10 min. |
| User closes browser mid-run | SSE closes | The `asyncio.Task` keeps running. New tab can poll `/api/run/status` and resume the SSE while the run is active. |
| Missing required config var | App startup check | Refuse to boot. Helpful error message. |

## 9. Observability

- `inference_audit` table replaces `reports/commands.log`. One row per
  inference call with prompt hash (not raw prompt) and token usage.
  Retained 30 days.
- Structured `logger.info({...})` for run start/end, visible via
  `heroku logs --tail`.
- No Datadog / Sentry / external APM in scope.

## 10. Security

- `SESSION_SECRET` signs the session cookie (HMAC-SHA256). Cookie is
  `HttpOnly`, `Secure`, `SameSite=Lax`.
- SF tokens never appear in logs or in `inference_audit`. Bearer
  injection happens at request time inside `mcp_path.build` and
  `native_tools.*`.
- Frontend continues to never use `innerHTML` with interpolated data
  (existing convention).
- Heroku Inference key is server-side only.
- ECA must add `https://token-comparison-tool-cb60c8f1dcc3.herokuapp.com/callback`
  to its callback URL list; existing `localhost` callback can stay or
  be removed.

## 11. Testing

| Test file | Change |
|---|---|
| `tests/test_runner.py` | Renamed → `test_messages_runner.py`. Mocks `anthropic.Anthropic`. Covers Native single-tool path, multi-turn loop, MCP path uses `mcp_servers` not `tools`, usage aggregation across turns, max-turns abort, tool error recorded with `is_error`. |
| `tests/test_native_tools.py` | NEW. Mocks `httpx.AsyncClient`; verifies each tool builds the right REST URL, applies the bearer header, parses the response. |
| `tests/test_db.py` | NEW. `pytest-postgresql` fixture-spawned local PG. Verifies idempotent migrations, sessions/reports round-trip. |
| `tests/test_sf_auth.py` | Updated. Drop localhost-only assertion; assert `https://*.herokuapp.com/callback` accepted. Token persistence test moves from filesystem to DB fixture. |
| `tests/test_report_loader.py`, `test_analysis.py`, `test_report.py`, `test_recommendations.py` | Untouched. |
| `tests/test_api.py` | FastAPI `TestClient` with fake DB session and stub `messages_runner` returning canned `RunResult`s. Asserts SSE heartbeats are emitted. |
| `tests/test_e2e_smoke.py` | NEW. Hits real Heroku Inference (haiku, cheapest) on a single trivial scenario. Skipped unless `RUN_INFERENCE_E2E=1`. Catches Inference contract drift before deploys. |

Estimated final test count: ~95.

## 12. Open questions / risks

- **Does Heroku Managed Inference forward `mcp_servers` upstream?** If
  it doesn't, the MCP-path fallback is to register the same MCP tool
  names locally and proxy each tool call to the gateway server-side.
  Same end-to-end token measurement, just one extra hop. We'll verify
  in the implementation phase before committing to one approach.
- **Cost per benchmark.** Inference is metered. A 6-scenario × 2-path
  × 3-run benchmark on opus is ~36 multi-turn loops. We will document
  approximate cost in the README once we measure it on a real run.
- **OAuth UX.** First-time users will see a Salesforce consent screen.
  Subsequent visits read the cached token from `sessions` and skip
  consent (until expiry).
- **Dyno restart mid-run.** A long benchmark interrupted by a deploy
  or platform restart is lost. Mitigation in the implementation plan
  (task: persist progress events, render "interrupted" state on
  reload) is acceptable for v1.

## 13. Out of scope (to revisit later)

- Multi-user / per-user-ECA mode.
- Worker dyno + Redis pub/sub for multi-user concurrency.
- A "model shootout" comparison that uses all three Inference addons.
- Server-rendered PDF export (current PDF export is browser-side via
  print-to-pdf; can stay).
- Migration off vanilla-JS SPA.
