# pgops-mcp

> A production-grade MCP server that gives AI agents safe, audited, expert-level control
> over a real PostgreSQL database and the Docker stack around it â€” no shell commands,
> no Python scripts, just tools.

## Why

Existing Postgres MCP servers are thin query wrappers: introspect + SELECT. None handle
migrations with lock-impact analysis, none diagnose performance from `EXPLAIN` +
`pg_stat_statements`, none understand the containerized environment the database lives in.
Agents operating databases today are flying blind and unsafe.

`pgops-mcp` is the operations brain: **schema intelligence â†’ guarded queries â†’ migration
engine â†’ performance diagnosis â†’ environment awareness**, with a safety architecture that
makes every action classifiable, confirmable, and auditable.

## Tool surface (v0.1)

| Group | Tools |
|---|---|
| Schema | `schema.inspect` |
| Queries | `query.read`, `query.write` (guarded), `query.explain` (parsed plan + verdict) |
| Performance | `index.advise`, `db.health` |
| Migrations | `migration.plan` (dry-run + lock analysis), `migration.apply`, `migration.history` |
| Environment | `env.topology`, `env.correlate`, `container.logs`, `container.stats` |
| Gated | `container.restart`*, `container.exec`* |

\* Not registered at all unless the server runs with `--approval-mode`, and even then
each call needs a confirmation token. `container.exec` additionally enforces a read-only
diagnostic command allowlist â€” it does not offer a shell. The Docker socket is
root-equivalent on the host, so the default is read-only access.

## Safety model (the core differentiator)

- Separate read-only / read-write connection roles; tools bind to the right role
- Statement classification before execution â€” unbounded `DELETE`/`UPDATE` blocked
- Destructive actions require explicit confirmation tokens
- Every executed statement lands in an append-only audit log with timing and verdict
- Runaway-query cancellation with timeout tiers

## MCP surface

| Primitive | What's here |
|---|---|
| **Tools** | 13 â€” schema, query, explain, advise, migrate, environment |
| **Resources** | `pgops://schema`, `schema/summary`, `schema/{table}`, `health`, `migrations`, `audit/recent`, `config` |
| **Prompts** | `diagnose-slow-query`, `plan-safe-migration`, `incident-triage`, `review-index-health`, `explain-safety-model` |
| **Elicitation** | Dangerous actions ask the **user** directly, not via the agent; confirmation tokens are the fallback |
| **Progress / logging** | Best-effort notifications during long operations |

## Remote access & agent tokens

stdio needs no auth â€” the server is a subprocess your client spawns, with no open port.
HTTP does, so it refuses to start without a key:

```bash
pgops-mcp keygen                                    # RS256 keypair
pgops-mcp issue-token --subject my-agent            # read-only by default
pgops-mcp issue-token --subject deploy-bot --scope pgops:read --scope pgops:write
pgops-mcp scopes                                    # which scope each tool needs

pgops-mcp --transport http --public-key ~/.pgops/keys/pgops_public.pem
```

The server holds only the **public** key, so it can verify tokens but never mint them.
Scopes (`pgops:read` / `pgops:write` / `pgops:admin`) map to the same danger tiers as the
guardrails, and a tool with no scope entry requires `admin` â€” deny by default. Binds
loopback unless you say otherwise.

## Quickstart

See **[SETUP.md](SETUP.md)** for the complete guide â€” install, configuration, client
wiring (Claude Desktop / Cursor / VS Code / Inspector / HTTP), and troubleshooting.

```bash
uv sync
cp .env.example .env      # then set PGOPS_DSN
uv run pgops-mcp --selfcheck --dsn "postgresql://user:pass@localhost:5432/mydb"
uv run pgops-mcp            # stdio transport for Claude Desktop / Cursor / VS Code
```

Add to Claude Desktop:

```json
{
  "mcpServers": {
    "pgops": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/pgops-mcp", "pgops-mcp"]
    }
  }
}
```

## Docs

**For users:**

- **[docs/API.md](docs/API.md)** — full tool catalog: parameters, returns, error codes, scopes
- [docs/BENCHMARKS.md](docs/BENCHMARKS.md) — what the benchmarks measure and what they are compared against
- [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md) — first 15 minutes, guided tour
- [SETUP.md](SETUP.md) — full setup guide: config, clients, HTTP auth, observability, troubleshooting
- [.env.example](.env.example) — every environment variable, documented

**Internal (design & process):**

- [internal/PRD.md](internal/PRD.md), [internal/SPEC.md](internal/SPEC.md) — product requirements & phased spec
- [internal/ARCHITECTURE.md](internal/ARCHITECTURE.md), [internal/SYSTEM_DESIGN.md](internal/SYSTEM_DESIGN.md) — design + rendered diagrams
- [internal/adr/](internal/adr/) — architecture decision records
- [internal/flow.md](internal/flow.md) — living progress log
- [internal/interview_prep.md](internal/interview_prep.md) — Q&A companion

## Status

**Phases 0â€“6b complete** (371 tests, every guardrail, verdict and lock-impact rule proven
against real Postgres via testcontainers â€” no mocks â€” plus end-to-end suites driving the
server as a real MCP subprocess over stdio and as an authenticated HTTP server, verified
through the MCP Inspector).

| Phase | State | Tools |
|---|---|---|
| 0 Â· Bootstrap | âœ… | seeded dev stack (1.2M-row `orders`), CI, lint/type gates |
| 1 Â· Connection core + read path | âœ… | `schema.inspect`, `query.read`, `db.health` |
| 2 Â· Write path + safety | âœ… | `query.write`, guardrails, confirmation tokens, audit log |
| 3 Â· Performance brain | âœ… | `query.explain` (plan verdicts), `index.advise` |
| 4 Â· Migration engine | âœ… | `migration.plan` (lock analysis + dry run), `apply`, `history` |
| 5 Â· Docker layer | âœ… | `env.topology`, `env.correlate`, `container.logs/stats/restart/exec` |
| 6a Â· MCP completeness | âœ… | resources, prompts, elicitation, progress |
| 6b Â· Remote + auth | âœ… | HTTP transport, JWT, scoped agent tokens, keygen CLI |
| 6c Â· Packaging | next | PyPI, Smithery, MCP registry |

`migration.rollback` is deliberately still open â€” see [`docs/API.md`](docs/API.md).

Sample of what `migration.plan` returns for a type change on the 1.2M-row `orders`:

```
ALTER TABLE "orders" ALTER COLUMN "total_cents" TYPE bigint
  op=table_rewrite  risk=high  estimate=4800ms  confidence=medium
  why:   rewrites every row and rebuilds every index, holding AccessExclusiveLock
  SAFER: add a new column of the target type, backfill in batches, sync with a
         trigger, swap the names, then drop the old column
```

Quickstart the dev database (host port **5435**, to avoid colliding with a local
Postgres on 5432):

```bash
docker compose up -d
export PGOPS_DSN="postgresql://pgops:pgops_dev@localhost:5435/pgops_demo"
uv run pgops-mcp --selfcheck
```
