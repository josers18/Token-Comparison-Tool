# Token Comparison Tool

A FastAPI + vanilla-JS web tool that benchmarks **token cost** between two
ways of invoking Salesforce operations from Claude:

- **Path A — Native:** Claude Code calls Salesforce APIs directly via the
  `sf` CLI. No MCP servers loaded.
- **Path B — MCP:** Claude Code calls the same operations through the
  Salesforce-hosted MCP servers (`salesforce_crm` and `data_cloud_queries`).

Both paths run the same prompt against the same model and the same org.
The only axis of variance is the tool provider. Per-run telemetry is read
from `claude -p --output-format json` and rendered as a side-by-side
comparison with verdict, hero metrics, turn-by-turn trace, and an
auto-generated executive summary.

## Features

- **Scenario catalog** — six curated scenarios spanning Sales Cloud and
  Data Cloud, ranging from simple SOQL reads to multi-DMO Customer 360
  joins.
- **Free-format mode** — write your own prompt and run it through both
  paths from a textarea.
- **Live progress** — Server-Sent Events stream every run as it
  completes; UI updates in place.
- **Editorial summary** — auto-generated headline, "When Native wins / When
  MCP wins" framework, cost-at-scale extrapolation.
- **Export** — markdown report download or full PDF (catalog + summary).

## Prerequisites

- Python 3.11+
- [Claude Code](https://docs.claude.com/en/docs/claude-code) installed and
  logged in (`claude --version` / `claude auth status`)
- [Salesforce CLI](https://developer.salesforce.com/tools/salesforcecli)
  installed and authenticated to an org (`sf org list`)
- A Salesforce External Client App (ECA) with `mcp_api`, `cdp_api`, and
  `refresh_token` scopes — see `.env.example` for the rest of the setup

## Quickstart

```bash
cp .env.example .env.local
# fill in SF_CLIENT_ID and SF_CLIENT_SECRET from your ECA

./run.sh
```

Opens at <http://localhost:8000>. Click **Connect Salesforce** to
authorize via OAuth (a browser window opens), then **Run benchmark** to
execute the scenario catalog, or use the **Free-format scenario** card at
the bottom to run a custom prompt.

## How it works

- Scenarios live as YAML files in `scenarios/` — adding one is zero-code.
- The `claude -p` invocation is centralised in `src/token_compare/runner.py`.
  Both paths call the same helper; only the `--mcp-config` and
  `--allowedTools` flags differ.
- Token + cost telemetry is extracted from the `claude -p` JSON output,
  preferring `modelUsage` (aggregate across all turns) over `usage` (last
  turn only).
- Reports go to `reports/benchmark-YYYY-MM-DD-HHMM.md` (gitignored). The
  ten most recent are retained.

## Design spec

`docs/superpowers/specs/2026-05-04-token-comparison-tool-design.md`

## License

MIT — see `LICENSE`.
