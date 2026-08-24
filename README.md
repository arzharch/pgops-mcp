# pgops-mcp

> A production-grade MCP server that gives AI agents safe, audited, expert-level control
> over a real PostgreSQL database and the Docker stack around it — no shell commands,
> no Python scripts, just tools.

## Why

Existing Postgres MCP servers are thin query wrappers: introspect + SELECT. None handle
migrations with lock-impact analysis, none diagnose performance from `EXPLAIN` +
`pg_stat_statements`, none understand the containerized environment the database lives in.
Agents operating databases today are flying blind and unsafe.

`pgops-mcp` is the operations brain: **schema intelligence → guarded queries → migration
engine → performance diagnosis → environment awareness**, with a safety architecture that
makes every action classifiable, confirmable, and auditable.

## Tool surface (v0.1)

| Group | Tools |
|---|---|
| Schema | `schema.inspect` |
| Queries | `query.read`, `query.write` (guarded), `query.explain` (parsed plan + verdict) |
| Performance | `index.advise`, `db.health` |
| Migrations | `migration.plan` (dry-run + lock analysis), `migration.apply`, `migration.history` |
| Environment | `env.topology`, `container.logs`, `container.stats`, `container.restart`* |

\* gated behind approval mode.

## Safety model (the core differentiator)

- Separate read-only / read-write connection roles; tools bind to the right role
- Statement classification before execution — unbounded `DELETE`/`UPDATE` blocked
- Destructive actions require explicit confirmation tokens
- Every executed statement lands in an append-only audit log with timing and verdict
- Runaway-query cancellation with timeout tiers

## Quickstart

```bash
uv sync
# point at your local Postgres in Docker:
export PGOPS_DSN="postgresql://user:pass@localhost:5432/mydb"
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

- [`docs/PRD.md`](docs/PRD.md) — what & why, goals, non-goals
- [`docs/SPEC.md`](docs/SPEC.md) — phased technical spec with hard gates
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — system design + trade-offs
- [`docs/TOOLS.md`](docs/TOOLS.md) — full tool catalog with schemas & examples
- [`docs/adr/`](docs/adr/) — architecture decision records
- [`docs/flow.md`](docs/flow.md) — living progress log
- [`docs/interview_prep.md`](docs/interview_prep.md) — growing interview Q&A

## Status

**Phases 0–4 complete** (243 tests, every guardrail, verdict and lock-impact rule proven
against real Postgres via testcontainers — no mocks — plus an end-to-end suite that
drives the server as a real MCP subprocess over stdio).

| Phase | State | Tools |
|---|---|---|
| 0 · Bootstrap | ✅ | seeded dev stack (1.2M-row `orders`), CI, lint/type gates |
| 1 · Connection core + read path | ✅ | `schema.inspect`, `query.read`, `db.health` |
| 2 · Write path + safety | ✅ | `query.write`, guardrails, confirmation tokens, audit log |
| 3 · Performance brain | ✅ | `query.explain` (plan verdicts), `index.advise` |
| 4 · Migration engine | ✅ | `migration.plan` (lock analysis + dry run), `apply`, `history` |
| 5 · Docker layer | next | `env.topology`, `container.logs/stats` |

`migration.rollback` is deliberately still open — see [`docs/TOOLS.md`](docs/TOOLS.md).

Sample of what `migration.plan` returns for a type change on the 1.2M-row `orders`:

```
ALTER TABLE "orders" ALTER COLUMN "total_cents" TYPE bigint
  op=table_rewrite  risk=high  estimate=4800ms  confidence=medium
  why:   rewrites every row and rebuilds every index, holding AccessExclusiveLock
  SAFER: add a new column of the target type, backfill in batches, sync with a
         trigger, swap the names, then drop the old column
```

Quickstart the dev database (host port **5433**, to avoid colliding with a local
Postgres on 5432):

```bash
docker compose up -d
export PGOPS_DSN="postgresql://pgops:pgops_dev@localhost:5433/pgops_demo"
uv run pgops-mcp --selfcheck
```
