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
| Environment | `env.topology`, `env.correlate`, `container.logs`, `container.stats` |
| Gated | `container.restart`*, `container.exec`* |

\* Not registered at all unless the server runs with `--approval-mode`, and even then
each call needs a confirmation token. `container.exec` additionally enforces a read-only
diagnostic command allowlist — it does not offer a shell. The Docker socket is
root-equivalent on the host, so the default is read-only access.

## Safety model (the core differentiator)

- Separate read-only / read-write connection roles; tools bind to the right role
- Statement classification before execution — unbounded `DELETE`/`UPDATE` blocked
- Destructive actions require explicit confirmation tokens
- Every executed statement lands in an append-only audit log with timing and verdict
- Runaway-query cancellation with timeout tiers

## MCP surface

| Primitive | What's here |
|---|---|
| **Tools** | 13 — schema, query, explain, advise, migrate, environment |
| **Resources** | `pgops://schema`, `schema/summary`, `schema/{table}`, `health`, `migrations`, `audit/recent`, `config` |
| **Prompts** | `diagnose-slow-query`, `plan-safe-migration`, `incident-triage`, `review-index-health`, `explain-safety-model` |
| **Elicitation** | Dangerous actions ask the **user** directly, not via the agent; confirmation tokens are the fallback |
| **Progress / logging** | Best-effort notifications during long operations |

## Remote access & agent tokens

stdio needs no auth — the server is a subprocess your client spawns, with no open port.
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
guardrails, and a tool with no scope entry requires `admin` — deny by default. Binds
loopback unless you say otherwise.

## Quickstart

See **[SETUP.md](SETUP.md)** for the complete guide — install, configuration, client
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

- [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md) — **new here? start here** — first 15 minutes, guided
- [`SETUP.md`](SETUP.md) — full setup guide: config, clients, HTTP auth, troubleshooting
- [`.env.example`](.env.example) — every environment variable, documented

- [`docs/PRD.md`](docs/PRD.md) — what & why, goals, non-goals
- [`docs/SPEC.md`](docs/SPEC.md) — phased technical spec with hard gates
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — system design + trade-offs
- [`docs/SYSTEM_DESIGN.md`](docs/SYSTEM_DESIGN.md) — rendered architecture diagrams (Mermaid — renders natively on GitHub/GitLab/VS Code; importable into Lucidchart, draw.io, or Mermaid Live via [mermaid.live](https://mermaid.live))
- [`docs/TOOLS.md`](docs/TOOLS.md) — full tool catalog with schemas & examples
- [`docs/adr/`](docs/adr/) — architecture decision records (incl. [ADR-006](docs/adr/ADR-006.md): the three-tier scaling path)
- [`docs/flow.md`](docs/flow.md) — living progress log
- [`docs/interview_prep.md`](docs/interview_prep.md) — growing interview Q&A

## Status

**Phases 0–6b complete** (371 tests, every guardrail, verdict and lock-impact rule proven
against real Postgres via testcontainers — no mocks — plus end-to-end suites driving the
server as a real MCP subprocess over stdio and as an authenticated HTTP server, verified
through the MCP Inspector).

| Phase | State | Tools |
|---|---|---|
| 0 · Bootstrap | ✅ | seeded dev stack (1.2M-row `orders`), CI, lint/type gates |
| 1 · Connection core + read path | ✅ | `schema.inspect`, `query.read`, `db.health` |
| 2 · Write path + safety | ✅ | `query.write`, guardrails, confirmation tokens, audit log |
| 3 · Performance brain | ✅ | `query.explain` (plan verdicts), `index.advise` |
| 4 · Migration engine | ✅ | `migration.plan` (lock analysis + dry run), `apply`, `history` |
| 5 · Docker layer | ✅ | `env.topology`, `env.correlate`, `container.logs/stats/restart/exec` |
| 6a · MCP completeness | ✅ | resources, prompts, elicitation, progress |
| 6b · Remote + auth | ✅ | HTTP transport, JWT, scoped agent tokens, keygen CLI |
| 6c · Packaging | next | PyPI, Smithery, MCP registry |

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
