# Getting Started â€” a new user's first 15 minutes

This is the walkthrough for someone who has never seen pgops-mcp before. No prior MCP
knowledge assumed. (Full setup detail lives in [SETUP.md](SETUP.md); this page is the
guided tour.)

---

## What you're about to run

pgops-mcp is a **server** that your AI assistant (Claude Desktop, Cursor, VS Code)
launches as a subprocess. The assistant then gains tools it can call â€” safely â€” against
your PostgreSQL database. You don't interact with the server directly; you talk to your
assistant in plain English and it uses the tools.

```
You â”€â”€talks toâ”€â”€> Claude/Cursor â”€â”€calls toolsâ”€â”€> pgops-mcp â”€â”€> your Postgres
                                                    â”‚
                                                    â””â”€â”€> audit log (~/.pgops/audit.jsonl)
```

---

## Step 1 â€” Prerequisites check

```bash
python --version   # need 3.12+
uv --version       # need uv; pip install uv if missing
psql "$YOUR_DSN" -c "SELECT 1"   # confirm Postgres reachable
```

No database handy? Skip ahead â€” Step 3 spins one up.

## Step 2 â€” Install

```bash
git clone <repo-url> pgops-mcp && cd pgops-mcp
uv sync
```

That's the whole install. `uv sync` creates the virtualenv and installs everything.

## Step 3 â€” Get a database (skip if you have one)

```bash
docker compose up -d
```

This starts Postgres 16 with `pg_stat_statements` enabled and ~1.2M rows of demo data.
Your DSN is:

```
postgresql://pgops:pgops_dev@localhost:5435/pgops_demo
```

(Note port **5435**, not 5432.)

## Step 4 â€” Sanity check before wiring anything

```bash
uv run pgops-mcp --selfcheck --dsn "postgresql://pgops:pgops_dev@localhost:5435/pgops_demo"
```

Expected:

```
readonly pool: OK
tables in public schema: 3
  - customers: ~30000 rows, ...
  - orders: ~1200000 rows, ...
  - products: ~500 rows, ...
```

If that works, the server can reach your database. Everything after this is just
connecting a client.

## Step 5 â€” Connect your AI client

### Claude Desktop / Cursor / VS Code

Find your client's MCP config file and add:

```json
{
  "mcpServers": {
    "pgops": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/pgops-mcp", "pgops-mcp"],
      "env": { "PGOPS_DSN": "postgresql://pgops:pgops_dev@localhost:5435/pgops_demo" }
    }
  }
}
```

- Config locations: Claude Desktop â†’ `claude_desktop_config.json` (Settings â†’ Developer);
  Cursor â†’ `.cursor/mcp.json`; VS Code â†’ `.vscode/mcp.json`.
- Use an **absolute** path for `--directory`.
- Restart the client after saving.

### Or try it without any client: MCP Inspector

```bash
npx @modelcontextprotocol/inspector --config inspector.config.json
```

Opens a browser UI where you can click through every tool manually â€” useful for seeing
what the agent will see.

## Step 6 â€” Actually use it

Talk to your assistant normally. Things to try, roughly in order of impressiveness:

1. **"Show me the schema"**
   â†’ calls `schema.inspect`. You'll get tables, columns, sizes.

2. **"How healthy is the database?"**
   â†’ calls `db.health`: connection counts, cache hit ratio, dead tuples, lock waits.

3. **"Why is this query slow? SELECT * FROM orders WHERE status = 'paid'"**
   â†’ calls `query.explain`, which parses the plan into verdicts like
   *"sequential scan examined 1,200,000 rows"* and *"sort spilled to disk"*.

4. **"Add a nullable note column to orders"**
   â†’ calls `migration.plan`. Read what comes back: each step carries a **lock impact
   estimate** ("metadata-only, microseconds") or a warning ("this rewrites the whole
   table"). Nothing has been applied yet â€” planning is free and safe.

5. **The safety demo â€” do this one on purpose:**

   > "Delete all rows from orders"

   Watch what happens: the tool **refuses** with `CONFIRMATION_REQUIRED` and explains
   why ("no WHERE clause, would affect every row"). Your assistant must relay that to
   you, and only if *you* approve does it proceed. This is the core value proposition:
   the architecture stops the bad command even though the model was willing to send it.

6. **Check the audit trail:**

   ```bash
   tail ~/.pgops/audit.jsonl
   ```

   Every executed statement *and every refusal* is there, with timing, row counts,
   verdicts, and who/what approved them.

---

## Common first-run issues

| Symptom | Fix |
|---|---|
| Client shows no tools | Check the client's MCP logs; usually a wrong `--directory` path |
| `DSN_MISSING` at startup | The `env` block in the client config is what counts â€” shell exports don't reach the spawned process |
| `CONNECTION_FAILED` | Test the DSN directly with `psql` first |
| Tests fail | They need Docker running (testcontainers) |

More in [SETUP.md Â§Troubleshooting](SETUP.md#troubleshooting).

---

## Where to go next

- [`docs/API.md`](API.md) â€” full catalog of all 15 tools with parameters
- [`docs/SYSTEM_DESIGN.md`](../internal/SYSTEM_DESIGN.md) â€” how the safety pipeline works under the hood
- [Remote/team setup](SETUP.md#http-transport-remote-agents) â€” HTTP transport + JWT tokens for agents
