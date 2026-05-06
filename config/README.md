# MCP config

`sf-mcp.json` is passed to `claude -p --mcp-config` on Path B runs
(see `src/token_compare/runner.py`).

## Environment variable interpolation

The file uses `${VAR}` interpolation. Claude Code substitutes from the
shell environment at startup, so export the variables before running —
or put them in `.env` at the repo root and source it (`set -a; . .env; set +a`).
See `.env.example` for the required variable names.

> Note: this file uses bare `${VAR}` (Claude Code syntax), not `${env:VAR}`
> (Cursor's syntax). Don't paste Cursor's `.cursor/mcp.json` verbatim.

## Configured servers

- **`salesforce_crm`** — `…/platform/mcp/v1/platform/sobject-all` — standard
  and custom sObject CRUD/describe tools for the authenticated org.
- **`data_cloud_queries`** — `…/platform/mcp/v1/data/data-cloud-queries` —
  Data Cloud DMO/DLO queries, segments, calculated insights.

Both servers share `SF_ACCESS_TOKEN` in their
`Authorization: Bearer …` header. The OAuth flow built into this app
fetches that token automatically — see `src/token_compare/sf_auth.py`.

Do not commit real credentials. `.env` is gitignored; `.env.example`
is the template.
