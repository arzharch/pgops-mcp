# Setup Guide

Complete setup for pgops-mcp — from a clean machine to a working MCP server in Claude
Desktop, Cursor, VS Code, or the MCP Inspector.

---

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) | latest | `pip install uv` or `curl -LsSf https://astral.sh/uv/install.sh \| sh` — brings its own Python |
| PostgreSQL | 16+ | local, Docker, or remote — anything reachable via a DSN |
| Docker | any recent | only for the `env.*` / `container.*` tools and the dev seed stack |
| Node.js | 18+ | only for the MCP Inspector (optional) |

Python is not a separate prerequisite: `uvx` fetches a suitable interpreter itself, and
the container image carries its own.

---

## 1. Install

pgops-mcp is an MCP **server**, not a library — there is nothing to import and nothing
to add to your project's dependencies. Pick a delivery method:

**`uvx` (recommended).** Nothing is installed permanently; each run fetches a pinned
environment:

```bash
uvx pgops-mcp --selfcheck --dsn "postgresql://user:pass@localhost:5432/mydb"
```

**Container**, if you would rather keep a Python toolchain off this machine:

```bash
docker run --rm -e PGOPS_DSN="postgresql://user:pass@host.docker.internal:5432/mydb"   ghcr.io/arzharch/pgops-mcp:latest --selfcheck
```

**Persistent install**, if you want a `pgops-mcp` on your PATH:

```bash
uv tool install pgops-mcp
```

To work on pgops-mcp itself rather than use it, see
[CONTRIBUTING.md](CONTRIBUTING.md) — that is the `git clone` + `uv sync` path.

---

## 2. Configure

Copy the example env file and point it at your database:

```bash
cp .env.example .env
# then edit .env and set PGOPS_DSN
```

The only **required** variable is `PGOPS_DSN`. Everything else has a safe default — see
the comments in `.env.example` for what each knob does and when to change it.

> `.env` is gitignored. Never commit real credentials.

### Quick sanity check

```bash
uv run pgops-mcp --selfcheck --dsn "postgresql://user:pass@localhost:5432/mydb"
```

Expected output:

```
readonly pool: OK
tables in public schema: 3
  - customers: ~30000 rows, ...
  - orders: ~1200000 rows, ...
  - products: ~500 rows, ...
```

If this fails, the DSN is wrong or Postgres isn't reachable — fix that before continuing.

---

## 3. Run against the seeded dev stack (optional)

If you don't have a database handy, spin up the demo one:

```bash
docker compose up -d        # Postgres 16 + pg_stat_statements + ~1.2M-row orders table
```

DSN for the dev stack:

```
postgresql://pgops:pgops_dev@localhost:5435/pgops_demo
```

Note the port is **5435**, not 5432 — chosen deliberately because lower ports were
already bound by other Postgres instances on the original dev machine.

---

## 4. Connect a client

### Claude Desktop / Cursor / VS Code (stdio)

Add to your client's MCP config (e.g. `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "pgops": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/pgops-mcp", "pgops-mcp"],
      "env": {
        "PGOPS_DSN": "postgresql://user:pass@localhost:5432/mydb"
      }
    }
  }
}
```

Use an absolute path for `--directory`. Restart the client after editing.

Environment variables from `.env` are **not** loaded automatically under stdio — the
client spawns the process with its own environment, so pass `PGOPS_DSN` in the `env`
block as shown above.

### MCP Inspector

```bash
npx @modelcontextprotocol/inspector --config inspector.config.json
```

This opens the web UI preconfigured to launch pgops-mcp over stdio with the dev-stack
DSN. Edit `inspector.config.json` to point at a different database.

CLI mode (no browser):

```bash
npx @modelcontextprotocol/inspector --cli --config inspector.config.json --server pgops --method tools/list
npx @modelcontextprotocol/inspector --cli --config inspector.config.json --server pgops \
  --method tools/call --tool-name query.read --tool-arg sql="SELECT count(*) FROM orders"
```

### HTTP transport (remote agents)

stdio has no auth because there's no remote caller. HTTP does, so it refuses to start
without a key:

```bash
# 1. Generate an RS256 keypair
uv run pgops-mcp keygen

# 2. Mint a token for an agent (read-only by default)
uv run pgops-mcp issue-token --subject my-agent
uv run pgops-mcp issue-token --subject deploy-bot --scope pgops:read --scope pgops:write

# 3. Start the server
uv run pgops-mcp --transport http --public-key ~/.pgops/keys/pgops_public.pem
```

The server holds only the **public** key — it can verify tokens but never mint them.
Binds `127.0.0.1` by default; pass `--host 0.0.0.0` explicitly if you really mean to
expose it.

Scope reference:

```bash
uv run pgops-mcp scopes
```

| Scope | Tools |
|---|---|
| `pgops:read` | schema.inspect, query.read, query.explain, db.health, index.advise, migration.plan, migration.history, env.*, container.logs/stats |
| `pgops:write` | query.write, migration.apply, migration.rollback |
| `pgops:admin` | container.restart, container.exec |

A tool with no scope entry requires `admin` — deny by default.

---

## 5. Verify end-to-end

From your connected client, try:

1. *"Show me the schema"* → triggers `schema.inspect`
2. *"How healthy is the database?"* → triggers `db.health`
3. *"Why is this query slow: SELECT * FROM orders WHERE status='paid'"* → triggers
   `query.explain` with plan verdicts
4. Attempt `DELETE FROM orders` without a WHERE clause → should be refused with
   `CONFIRMATION_REQUIRED` and a human-readable reason

If #4 executes instead of refusing, stop and check you're running the right server.

---

## 6. Scaling: what this is and isn't

pgops-mcp is a **local-first developer tool**, not a horizontally-scaled service. That's
a deliberate design decision, documented honestly in
[`docs/SYSTEM_DESIGN.md` §4](docs/SYSTEM_DESIGN.md#4-deployment-topology---what-scales-and-what-doesnt)
and [ADR-006](docs/adr/ADR-006.md):

- ✅ One engineer, one or a few databases, one or more MCP clients — the intended use,
  fully supported.
- ⚠️ Several clients sharing one server — works; writes serialize and the audit log is
  shared. Fine for a team pointing at a shared dev database.
- ❌ Multi-tenant SaaS / many users × many databases — not built. Auth identifies *who*
  but every caller shares one connection manager; plan/token state is in-memory so
  instances can't share it.

**Why local-first is the right default (a security argument, not a limitation):** when
the database is local, the operator credential lives on the user's own machine, under
their own account, with no network listener at all. A shared server buys team
collaboration at the price of a network attack surface that must be defended forever.
The transport-bound auth design means each deployment makes that trade deliberately.

If you do need the team-server tier, ADR-006 names exactly what must be built first:
per-session DSN isolation, a centralized audit sink, externalized plan/token state, and
per-subject rate limits. Don't deploy this as a multi-tenant service expecting isolation
it doesn't provide.

---

## 7. Observability (optional)

The server emits OpenTelemetry traces + metrics and serves liveness/readiness endpoints.
Everything is **off by default** — with no env vars set there is zero overhead beyond a
few dict lookups, and telemetry can never break a tool call.

```bash
# install the optional deps
uv sync --extra otel

# stand up a local trace backend (Jaeger all-in-one, OTLP gRPC on 4317)
docker run --rm -d --name pgops-jaeger -p 4317:4317 -p 16686:16686 \
  jaegertracing/all-in-one:latest

# run the server with telemetry + health endpoints
export PGOPS_OTEL_ENDPOINT=http://localhost:4317
export PGOPS_HEALTH_PORT=8080
uv run pgops-mcp --transport http --host 127.0.0.1 --port 8001 \
  --public-key keys/demo/pgops_public.pem
```

Then:

- **Traces:** open http://localhost:16686, pick service `pgops-mcp`. Every tool call is
  one span with `pgops.verdict` (`executed` / `refused` / `denied` / `failed`) and, on
  refusals, `pgops.error_code`. Scope denials are spans too — a spike usually means a
  misconfigured agent or a rotated token missing its scopes.
- **Metrics:** `pgops.tool.calls` (by tool, verdict, caller), `pgops.tool.duration`,
  `pgops.pool.timeouts`, `pgops.db.up`.
- **Health:** `GET :8080/health` (liveness — restart me if this fails) and
  `GET :8080/ready` (readiness — Postgres reachable right now; returns 503 when it
  isn't). Wire these into a compose healthcheck or process manager.

If Jaeger/collector is down, export failures are logged warnings; tool calls are
unaffected. The audit log remains the system of record — this layer is the *operational*
view (latency, error rates), complementary to the *forensic* view (who did what).

---

## Troubleshooting

**`DSN_MISSING` on startup**
No `PGOPS_DSN` in the environment. Under stdio, remember the client's `env` block is
what counts — a shell export in your terminal doesn't reach the spawned server.

**`CONNECTION_FAILED: could not connect readonly pool`**
Postgres unreachable or credentials wrong. Test the DSN directly:
`psql "$PGOPS_DSN" -c "SELECT 1"`.

**Tests fail with Docker errors**
The suite uses testcontainers — Docker must be running. On Windows, make sure Docker
Desktop is up before `uv run pytest`.

**Port 5435 already in use (dev stack)**
Another container is bound there. Either stop it or edit `docker-compose.yml`.

**Audit log location**
Default is `~/.pgops/audit.jsonl`. Override with `PGOPS_AUDIT_LOG`. Every executed
statement *and every refusal* lands here — this is the file an incident review reads.

**Client doesn't show the tools**
Check the client's MCP logs. The most common cause is a stray `print()` somewhere
corrupting the stdio protocol stream — pgops logs everything to stderr for exactly this
reason, but a wrapper script might not.
