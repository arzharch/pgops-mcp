# Flow — Living Progress Log

> Everything done on pgops-mcp, newest first. Updated in the same commit as any feature.
> Format: `[date] PHASE-N: what was done → why / notes`.

---

## 2026-08-24 · Phase 2 — Write path + safety architecture

- **PHASE-2:** `guardrails.py`: two independent mechanisms. (1) Unbounded-mutation
  detection — UPDATE/DELETE with no WHERE is refused. Detected from the sqlparse token
  stream, not `"where" in sql.lower()`, which would be fooled by
  `INSERT INTO log VALUES ('where')`, a column named `wherefore`, and
  `DELETE FROM orders -- WHERE id = 1` (all three are test cases). (2) Confirmation
  tokens: destructive/unknown statements are refused on first call and return a token.
- **PHASE-2:** Token binding is the non-obvious part. A token that only means "the user
  approved something" is forgeable-by-confusion — an agent could get approval for
  `DELETE FROM staging` and redeem it against `DELETE FROM orders`. Each token is bound
  to the SHA-256 of the exact statement it was issued for; redeeming against different
  SQL fails with `CONFIRMATION_MISMATCH` and — deliberately — does *not* consume the
  token, since the user's real pending approval is still legitimate. Tokens are
  single-use, TTL-bound (5 min default), minted with `secrets.token_urlsafe` (it's an
  authorization credential, not a random id), and in-memory only so a restart
  invalidates every outstanding approval.
- **PHASE-2:** `audit.py`: append-only JSONL at `~/.pgops/audit.jsonl` (home, not CWD —
  an MCP server is launched by the client with a working directory the user never
  chose). Every executed statement *and every refusal* is recorded: a blocked
  `DELETE FROM orders` is precisely the event an incident review needs, and a naive
  "log what we ran" design discards it. SQL stored with its SHA-256 so identical
  statements group without string-matching over embedded literals. An audit write
  failure logs loudly but never fails the tool call that already succeeded.
- **PHASE-2:** `tools/write.py` — `query.write`, ordered classify → guardrails → token →
  execute → audit. The token check sits *after* guardrail evaluation so a token can only
  unblock an already-identified specific risk; it is not a general "skip safety" flag.
  Executes inside an explicit transaction with `SET LOCAL statement_timeout`, so a
  cancelled statement rolls back rather than half-applying.
- **PHASE-2:** `--read-only` removes `query.write` from the advertised tool list
  entirely rather than registering it and refusing at call time — an agent cannot be
  tempted by a tool it was never told exists.

### Gate evidence

```
uv run pytest -q      # 115 passed
uv run ruff check .   # All checks passed!
uv run mypy src       # Success: no issues found in 14 source files
```

Against the live dev stack (real 1.2M-row `orders` table), driven through the actual
FastMCP server object:

```
orders before: 1200000
REFUSED: CONFIRMATION_REQUIRED | DELETE has no WHERE clause and would affect every row in the table
orders after refusal: 1200000          <- nothing deleted
TOKEN REUSE ON DIFFERENT SQL: CONFIRMATION_MISMATCH
BOUNDED UPDATE: {'rows_affected': 3, 'duration_ms': 110.0, 'audit_id': '...', 'classification': 'write'}
```

Resulting audit trail — refusal, blocked token-reuse, and execution all captured:

```json
{"verdict":"refused_pending_confirmation","sql":"DELETE FROM orders","detail":"DELETE has no WHERE clause and would affect every row in the table"}
{"verdict":"refused_bad_token","sql":"DROP TABLE orders","error_code":"CONFIRMATION_MISMATCH"}
{"verdict":"executed","sql":"UPDATE orders SET status = 'paid' WHERE id <= 3","rows_affected":3,"duration_ms":110.0}
```

---

## 2026-08-24 · Phase 1 review — production hardening

Audit of the Phase 1 code before starting Phase 2. Found four real defects, all with
regression tests now:

- **BUG (transport):** `schema.inspect(level="full")` was broken for every caller.
  `pg_constraint.contype` is Postgres's internal `"char"` type, which asyncpg decodes to
  Python `bytes` — `json.dumps` cannot encode it, so the tool failed at the MCP
  serialization boundary. It went unnoticed because the selfcheck and manual testing
  both only exercised `level="summary"`. Fixed by expanding `contype` to a readable
  label in SQL (`primary_key`, `foreign_key`, …) and routing all catalog output through
  `serialize_value`. `db.health` had the same latent defect (`dead_pct` is a numeric →
  `Decimal`), and its JSON test passed only because a freshly seeded container has no
  dead tuples — the test now generates churn first.
- **BUG (error leakage):** `schema.inspect` passed the table name into `$1::regclass`,
  which parses its input as an identifier expression — any name needing quoting
  (`"Order Items"`) raised a raw `InvalidNameError` that escaped the tool layer with a
  traceback, violating SPEC cross-cutting rule #2. Fixed by keying every catalog query
  off `pg_class.oid`, removing name parsing entirely.
- **BUG (error leakage, systemic):** each tool caught only `PgopsError`, which by
  definition catches only anticipated failures. Added `tool_boundary` — a decorator
  wrapping every tool that catches `Exception`, logs the full traceback to stderr, and
  returns a generic `INTERNAL_ERROR` to the caller.
- **BUG (liveness):** `pool.acquire()` has no default timeout — with every connection
  held by slow queries, further tool calls hung indefinitely with no error. Added
  `ConnectionManager.acquire_readonly()` with a bounded wait surfacing
  `POOL_EXHAUSTED`.

Quality improvements in the same pass:

- **N+1 removed:** `schema.inspect` ran 3 catalog queries *per table* (600 round trips
  for a 200-table schema at `level="full"`). Now 3 total via `= ANY($1::oid[])`.
- **Lock detection corrected:** replaced the hand-rolled `pg_locks` self-join with
  `pg_blocking_pids()`. The self-join is the version that circulates on blogs and is
  subtly wrong — it misses lock types not identified by locktype/relation (tuple,
  transactionid, advisory) and reports false positives for non-conflicting modes.
- **Logging to stderr, explicitly:** under stdio transport stdout *is* the MCP protocol
  channel; one stray log line corrupts the session in a way that looks like a client bug.
- **Tool names match docs:** were `schema_inspect_tool` etc. (derived from Python
  function names); now `schema.inspect`, `query.read`, `db.health` per TOOLS.md. A tool
  name is a public contract, not an implementation detail.
- **Test isolation:** the fixture used `TRUNCATE items` without `RESTART IDENTITY`, so
  re-seeded rows continued from the previous test's id high-water mark and assertions
  like `WHERE id <= 5` matched a different number of rows depending on test order.
- Also added: `py.typed` marker, config-resolution tests, and end-to-end tests through
  the real FastMCP server — the layer where both serialization bugs would have been
  caught the first time.

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

- [ ] Phase 3: `query.explain` (EXPLAIN plan parser + verdicts), `index.advise`
- [ ] Known gap to close: classifier can't see writes inside volatile functions
      (`SELECT my_func()`); needs a `pg_proc.provolatile` catalog lookup. Currently
      caught only by the read-only pool at execution time (ADR-001).
