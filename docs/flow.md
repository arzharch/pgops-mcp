# Flow — Living Progress Log

> Everything done on pgops-mcp, newest first. Updated in the same commit as any feature.
> Format: `[date] PHASE-N: what was done → why / notes`.

---

## 2026-08-24 · Phase 1 — Connection core + read path

- **PHASE-1:** `ConnectionManager` (`connections.py`): two asyncpg pools per DSN.
  readonly pool is eager (created at startup); every connection acquired from it runs
  `SET default_transaction_read_only = on`. This is the real enforcement mechanism —
  Postgres refuses writes at the executor level for a read-only transaction regardless
  of the DSN role's actual GRANTs, so a classifier bug can never get a write through the
  read path. readwrite pool is lazy (Phase 2 wires its first caller). Proven, not just
  asserted: `tests/test_connections.py` opens the readonly pool with the *superuser*
  test-container DSN and shows `INSERT` is still rejected.
- **PHASE-1:** `Classifier` (`classifier.py`, ADR-001): deny-by-default SQL
  classification — read / write / ddl / destructive / unknown. Allowlist shape, not
  blocklist: a statement must prove it's a pure read to be classified `read`. Built on
  `sqlparse` (pure-Python lexer) rather than `pglast`/libpg_query (rejected — C
  extension wheels per platform fight the <2min-install goal) or hand-rolled regex
  (rejected — can't reliably distinguish a string literal from a keyword, and CTEs need
  real tokenization). Catches writes hidden inside CTEs
  (`WITH x AS (INSERT ... RETURNING *) SELECT * FROM x`) by scanning every token for a
  `Keyword.DML` write, not just the leading keyword. Rejects multi-statement
  submissions outright (stacked-query injection shape). 26 table-driven cases in
  `tests/test_classifier.py`, including a string-literal false-positive check
  (`SELECT 'insert' AS label`).
- **PHASE-1:** `query.read` tool (`tools/query.py`): classifier gate (only `read`
  passes), row-limit enforcement via server-side cursor `.fetch(limit)` — no SQL
  rewriting/wrapping, since that breaks on EXPLAIN/CTEs/trailing semicolons and is one
  more place to string-manipulate untrusted SQL — and per-call `SET LOCAL
  statement_timeout` inside an explicit transaction, so a call's timeout tier can never
  leak onto the next caller reusing the same pooled connection.
- **PHASE-1:** `serialize.py`: one JSON-safety module for asyncpg row values (Decimal,
  datetime, UUID, bytea, ranges) shared by every tool that returns rows, instead of
  each tool inventing its own `str()`.
- **PHASE-1:** `schema.inspect` tool (`tools/schema.py`): summary/tables/full levels
  against `pg_catalog`/`information_schema` directly — no SQLAlchemy reflection
  dependency, no parsing `psql \d+` text output.
- **PHASE-1:** `db.health` tool (`tools/health.py`): connection counts by state, buffer
  cache hit ratio, dead-tuple bloat, long-running queries (>5s), waiting-lock chains —
  each finding carries a severity and a plain-language summary.
- **PHASE-1:** `__main__.py` wired: FastMCP server, three tools registered, `--dsn` /
  `--read-only` / `--selfcheck` CLI flags. `--selfcheck` connects, introspects, and
  prints a summary without starting the MCP transport — verified end-to-end against the
  live dev stack (1.2M-row `orders` table, correct counts and byte sizes).

### Gate evidence

```
uv run pytest -q         # 37 passed (classifier table-driven + testcontainers-backed
                          # connection/query tests against real Postgres 16)
uv run ruff check .      # All checks passed!
uv run mypy src          # Success: no issues found in 11 source files
uv run pgops-mcp --selfcheck   # against dev stack:
  readonly pool: OK
  tables in public schema: 3
    - customers: ~30000 rows, 6062080 bytes
    - orders: ~1200000 rows, 119717888 bytes
    - products: ~500 rows, 139264 bytes
```

Confirmed live through the actual FastMCP server object (not just the underlying
functions): `server.list_tools()` → `schema_inspect_tool`, `query_read_tool`,
`db_health_tool`; `server.call_tool('db_health_tool', {})` returned a real health
report against the dev database.

---

## 2026-08-24 · Phase 0 — Bootstrap complete

- **PHASE-0:** `dev/init.sql` seeded for real: ~30k customers, 500 products, ~1.2M
  orders via `generate_series`, with a skewed customer_id distribution (some customers
  own a disproportionate share of orders) so later phases have a real seq-scan /
  missing-index / lock-impact story instead of a toy table. Deliberately only one index
  exists (`orders.customer_id`) so `index.advise` (Phase 3) and lock-impact analysis
  (Phase 4) have something real to find.
- **PHASE-0:** `docker-compose.yml`: found port 5432 already bound by another local
  Postgres instance on this machine (a real "unfamiliar environment" collision, not a
  hypothetical) — remapped to host `5433`, added a `pg_isready` healthcheck, named the
  container so it doesn't collide with the many other same-named `postgres` containers
  already running locally.
- **PHASE-0:** `pyproject.toml`: `uv_build` needs `[tool.uv.build-backend] module-name`
  when the import package name (`pgops`) doesn't match the normalized project name
  (`pgops_mcp`) — `uv sync` failed with "Expected a Python module at
  src\pgops_mcp\__init__.py" until this was set explicitly.
- **PHASE-0:** Verified live: `docker compose up -d` → healthy, seed counts confirmed
  via `psql` inside the container (30000 / 500 / 1200000); `uv sync --extra dev` →
  `uv run pytest -q` passes.

### Gate evidence

```
docker exec pgops_dev_postgres psql -U pgops -d pgops_demo -c \
  "SELECT (SELECT count(*) FROM customers) c, (SELECT count(*) FROM products) p, \
          (SELECT count(*) FROM orders) o;"
   c   |  p  |    o
-------+-----+---------
 30000 | 500 | 1200000

uv run pytest -q      # 1 passed
```

---

## 2026-08-24 · Phase 0 — Inception

- **PHASE-0:** Project selected after marketplace research (official MCP registry,
  Smithery ~17k servers, mcp.so). Verified gaps: existing Postgres MCPs are thin
  query/introspection wrappers; none do migrations with lock analysis, performance
  diagnosis, or Docker environment awareness. Alternatives evaluated and rejected:
  Tally MCP (no license access), WhatsApp Business (user rejected), durable-jobs MCP
  (surface reads as "just cron"), Zoho (user rejected).
- **PHASE-0:** Wrote full docs set: PRD (goals G1–G5, FRs, acceptance criteria),
  SPEC (7 phases with hard gates), ARCHITECTURE (diagrams + decision table),
  TOOLS (full catalog), ADR-001..005.
- **PHASE-0:** Decided stack: Python 3.12+, FastMCP, asyncpg, docker SDK, pytest +
  testcontainers, uv. stdio transport first (ADR-002).
- **PHASE-0:** No code yet by design — implementation starts at Phase 1 of SPEC.md.

### Gate evidence

(none yet)

---

## Next up

- [ ] Phase 2: `query.write` + guardrails (unbounded UPDATE/DELETE detection,
      confirmation-token protocol) + audit log (`audit.py`)
- [ ] Phase 3: `query.explain` (EXPLAIN plan parser + verdicts), `index.advise`
