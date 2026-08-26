# pgops-mcp

<!-- mcp-name: io.github.arzharch/pgops-mcp -->

> **Let an AI assistant operate your PostgreSQL database — without letting it wreck
> anything.**

Point Claude, Cursor, or VS Code at a database and ask questions in plain English. Your
assistant gets 17 tools for inspecting schemas, running queries, diagnosing slow ones,
and planning migrations. What it does *not* get is a way to quietly destroy your data.

## What it actually looks like

Every number below is real output from the demo database in this repo — 1.2M rows,
114 MB.

> **You:** I want to index `orders.status`

> **Assistant:** *(calls `migration.plan`)*

```
CREATE INDEX "idx_orders_status" ON "orders" (status)
  risk=medium   estimate=4000ms   confidence=medium
  blocks:  writes yes, reads no
  SAFER:   CREATE INDEX CONCURRENTLY "idx_orders_status" ON "orders" (status)
```

> **Assistant:** A plain index build blocks writes for roughly 4 seconds. `CONCURRENTLY`
> is slower overall but keeps writes working. Which do you want?

The tool didn't return a raw plan for the model to interpret — it returned a **verdict**,
a duration estimate with an honest confidence level, and the safer alternative.

`query.explain` works the same way. Instead of handing back JSON, it says things like:

```
planner overestimated rows by 5833x (estimated 239,160, actual 40)
  -> run ANALYZE; if it persists the columns may be correlated, consider CREATE STATISTICS
```

### And when something is genuinely dangerous

> **You:** clear out the orders table

> **Assistant:** *(calls `query.write` with `DELETE FROM orders`)*

```
CONFIRMATION_REQUIRED
DELETE has no WHERE clause and would affect every row in the table
```

Where your client supports it, the approval prompt goes to **you** — not to the
assistant. Nothing runs until a human answers, and the refusal is written to the audit
log whether or not you approve.

That last part is the point. **The assistant cannot approve its own dangerous action,
because it is not the one being asked.** Where a client can't show a prompt, it degrades
to a single-use token bound to that exact statement — never to "allowed".

## Why this exists

Most Postgres MCP servers are thin query wrappers: introspect and `SELECT`. None handle
migrations with lock-impact analysis, none diagnose performance from `EXPLAIN` and
`pg_stat_statements`, and none understand the container the database runs in. Agents
operating databases today are doing it blind, and without guardrails.

`pgops-mcp` is the operations brain: **schema intelligence → guarded queries → migration
engine → performance diagnosis → environment awareness**, with a safety architecture that
makes every action classifiable, confirmable, and auditable.

**New here?** [docs/GETTING_STARTED.md](https://github.com/arzharch/pgops-mcp/blob/main/docs/GETTING_STARTED.md) is a 15-minute guided
tour that assumes no MCP knowledge.

## Tool surface

| Group | Tools |
|---|---|
| Schema | `schema.inspect` |
| Queries | `query.read`, `query.write` (guarded), `query.explain` (parsed plan + verdict) |
| Performance | `index.advise`, `db.health` |
| Migrations | `migration.plan` (dry-run + lock analysis), `migration.describe` (plain English), `migration.apply`, `migration.rollback`, `migration.history` |
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
| **Tools** | 17 — schema, query, explain, advise, migrate, environment |
| **Resources** | `pgops://schema`, `schema/summary`, `schema/{table}`, `health`, `migrations`, `audit/recent`, `config` |
| **Prompts** | `diagnose-slow-query`, `plan-safe-migration`, `incident-triage`, `review-index-health`, `explain-safety-model` |
| **Elicitation** | Dangerous actions ask the **user** directly, not via the agent; confirmation tokens are the fallback |
| **Sampling** | `migration.describe` turns English into a plan using *your* model — this server ships no API key |
| **Completions** | Table-name autocomplete for `pgops://schema/{table}` |
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

## Install

`pgops-mcp` is an MCP server, not a Python library — nothing in it is meant to be
imported, and `pgops.*` carries no API-stability promise. You install it the way you
install any MCP server: point your client at it.

**Claude Desktop / Cursor / VS Code:**

```json
{
  "mcpServers": {
    "pgops": {
      "command": "uvx",
      "args": ["pgops-mcp"],
      "env": { "PGOPS_DSN": "postgresql://user:pass@localhost:5432/mydb" }
    }
  }
}
```

`uvx` fetches and runs it in a throwaway environment — nothing to install first, and
nothing added to your own project's dependencies.

**Or run the container**, if you would rather not put a Python toolchain on the machine
that talks to your database:

```json
{
  "mcpServers": {
    "pgops": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-e", "PGOPS_DSN",
        "-v", "pgops-audit:/var/lib/pgops",
        "ghcr.io/arzharch/pgops-mcp:latest"
      ],
      "env": { "PGOPS_DSN": "postgresql://user:pass@host.docker.internal:5432/mydb" }
    }
  }
}
```

Two things the container changes: mount a volume at `/var/lib/pgops` or the audit log
dies with the container, and `localhost` inside a container is the container itself —
use `host.docker.internal` or a compose service name.

**Check the connection before wiring a client to it:**

```bash
uvx pgops-mcp --selfcheck --dsn "postgresql://user:pass@localhost:5432/mydb"
```

Both paths install the same server and are listed together in the
[MCP Registry](https://registry.modelcontextprotocol.io) entry — they fail for different
people. `uvx` needs nothing preinstalled but assumes the host may run Python; the
container assumes only Docker.

See **[SETUP.md](https://github.com/arzharch/pgops-mcp/blob/main/SETUP.md)** for configuration, HTTP transport, agent tokens and
troubleshooting, and [CONTRIBUTING.md](https://github.com/arzharch/pgops-mcp/blob/main/CONTRIBUTING.md) to run it from a source checkout.

## Docs

Links are absolute so they resolve from the PyPI project page as well as from GitHub.

**Using it**

| Doc | What's in it |
|---|---|
| [Getting started](https://github.com/arzharch/pgops-mcp/blob/main/docs/GETTING_STARTED.md) | First 15 minutes, no MCP knowledge assumed |
| [Tool reference](https://github.com/arzharch/pgops-mcp/blob/main/docs/API.md) | All 17 tools: parameters, returns, error codes, scopes |
| [Setup & configuration](https://github.com/arzharch/pgops-mcp/blob/main/SETUP.md) | Clients, HTTP auth, observability, troubleshooting |
| [Environment variables](https://github.com/arzharch/pgops-mcp/blob/main/.env.example) | Every knob, documented |
| [Security model](https://github.com/arzharch/pgops-mcp/blob/main/SECURITY.md) | What it can do, what it refuses, known limits |
| [Changelog](https://github.com/arzharch/pgops-mcp/blob/main/CHANGELOG.md) | What changed per release |

**How it works**

| Doc | What's in it |
|---|---|
| [Architecture](https://github.com/arzharch/pgops-mcp/blob/main/docs/ARCHITECTURE.md) | System design and trade-offs |
| [System design](https://github.com/arzharch/pgops-mcp/blob/main/docs/SYSTEM_DESIGN.md) | The safety pipeline, with diagrams |
| [Decision records](https://github.com/arzharch/pgops-mcp/blob/main/docs/adr/) | Why each choice was made, and what it cost |
| [Benchmarks](https://github.com/arzharch/pgops-mcp/blob/main/docs/BENCHMARKS.md) | What is measured, and against what |

**Contributing**

| Doc | What's in it |
|---|---|
| [Contributing](https://github.com/arzharch/pgops-mcp/blob/main/CONTRIBUTING.md) | Source checkout, gates, release process |
| [Module layout](https://github.com/arzharch/pgops-mcp/blob/main/LAYOUT.md) | What each module is for |

## Status

**Phases 0–6f complete** (436 tests, every guardrail, verdict and lock-impact rule proven
against real Postgres via testcontainers — no mocks — plus end-to-end suites driving the
server as a real MCP subprocess over stdio and as an authenticated HTTP server, verified
through the MCP Inspector).

| Phase | State | Tools |
|---|---|---|
| 0 · Bootstrap | ✅ | seeded dev stack (1.2M-row `orders`), CI, lint/type gates |
| 1 · Connection core + read path | ✅ | `schema.inspect`, `query.read`, `db.health` |
| 2 · Write path + safety | ✅ | `query.write`, guardrails, confirmation tokens, audit log |
| 3 · Performance brain | ✅ | `query.explain` (plan verdicts), `index.advise` |
| 4 · Migration engine | ✅ | `migration.plan` (lock analysis + dry run), `apply`, `rollback`, `history` |
| 5 · Docker layer | ✅ | `env.topology`, `env.correlate`, `container.logs/stats/restart/exec` |
| 6a · MCP completeness | ✅ | resources, prompts, elicitation, sampling, completions, progress |
| 6b · Remote + auth | ✅ | HTTP transport, JWT, per-tool scope enforcement, keygen CLI |
| 6c · Observability | ✅ | OTel spans/metrics, liveness/readiness endpoints (all optional) |
| 6d · Adversarial testing | ✅ | red-team suite, property-based tests, live evals in CI |
| 6e · Forensics | ✅ | `pgops-mcp replay` — the audit log as an executable record |
| 6f · Distribution | ✅ | PyPI package, container image, `server.json` for the MCP Registry |

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
