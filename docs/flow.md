# Flow — Living Progress Log

> Everything done on pgops-mcp, newest first. Updated in the same commit as any feature.
> Format: `[date] PHASE-N: what was done → why / notes`.

---

## 2026-08-24 · Phase 4 — Migration engine ⭐

Built against verified Postgres 16 behaviour, not recalled documentation. Probes run
first, then the code written to match:

```
CREATE INDEX CONCURRENTLY inside BEGIN  -> ERROR: cannot run inside a transaction block
ADD COLUMN b int NOT NULL DEFAULT 7     -> relfilenode UNCHANGED  (no rewrite)
ALTER COLUMN a TYPE bigint              -> relfilenode CHANGED    (full rewrite)
ADD COLUMN c int DEFAULT (random())     -> relfilenode CHANGED    (full rewrite)
```

- **PHASE-4:** `migrations/lock_analysis.py` — the differentiator. The key insight the
  module is built on: **all three ALTERs above take `AccessExclusiveLock`, so lock mode
  alone tells you almost nothing.** What separates a non-event from an outage is whether
  the operation *rewrites or scans* while holding it. Hence the constant-vs-volatile
  DEFAULT split — near-identical SQL, differing by a full table rewrite.
- **PHASE-4:** Risk is duration × *what is blocked*, not duration alone: the "high"
  threshold is 1s when reads are blocked and 5s when only writes are. Ranking them the
  same would either cry wolf about index builds or wave through a real outage.
- **PHASE-4:** Estimate rates calibrated by measurement (rewrite ~500k rows/s, index
  build ~650k rows/s on this machine) then **halved** — for a safety tool the dangerous
  direction to be wrong in is optimistic. Nothing that scales with table size may claim
  `high` confidence, since the rate depends on hardware we cannot see (ADR-004).
- **PHASE-4:** `migrations/diff.py` — target schema as JSON, not DDL: describing desired
  state is far less error-prone for an agent than writing migration SQL, and it lets the
  engine own ordering. Creations run outside-in (tables → columns → constraints →
  indexes), drops strictly in reverse. Type aliases are normalized (`int`/`integer`,
  `varchar`/`character varying`) because a raw string compare would emit a spurious
  `ALTER TYPE` — a full rewrite — for two identical types.
- **PHASE-4:** `allow_drops` defaults to **false**. A target that merely omits a table is
  far more likely to be a partial description than a request to destroy it.
- **PHASE-4:** `migrations/ledger.py` — the row is written `in_flight` **before** the DDL
  runs, not after. A ledger that inserts on success leaves no trace of a process killed
  mid-migration, and cannot distinguish "never started" from "half applied". A partial
  unique index allows retry-after-failure while making double-apply impossible.
- **PHASE-4:** `tools/migrations.py` — `plan` dry-runs every transactional step inside a
  doomed transaction, catching what static analysis cannot (a type that doesn't exist, a
  constraint existing data violates). Non-transactional steps (`CONCURRENTLY`) are
  reported as making the plan **not atomic** rather than letting the caller assume a
  guarantee that doesn't hold.

### Bugs found and fixed during Phase 4

- **The engine dropped its own ledger.** With `allow_drops=true` and a target that
  (reasonably) didn't mention it, the diff emitted `DROP TABLE pgops_migrations` — the
  migration destroyed the table recording it, then crashed with
  `relation "pgops_migrations" does not exist` while marking itself finished. Internal
  tables are now excluded from the diff and refused as a target.
- **`ADD CONSTRAINT` was misreported as a harmless column add.** The permissive
  "ADD `<name>` `<type>`" pattern (needed because the `COLUMN` keyword is optional)
  swallowed `ADD CONSTRAINT c CHECK (...)`, hiding a full-table validation scan behind a
  `metadata_only` verdict. Constraint clauses are now matched first.
- **Every duration this project reports was wrong on Windows.** `time.monotonic()` has
  15.625 ms resolution there — it measures a 10 ms sleep as `0.000 ms`. Caught in the
  ledger, which stored `duration_ms = 0` for a migration whose own timestamps were
  9.8 ms apart: the row disagreed with itself. `duration_ms` is forensic data in the
  audit log, so this was a real defect, not cosmetic. All four measurement sites now use
  `perf_counter` via `timing.py`; verified `duration_ms 18.238` against wall `18.031`.

### Gate evidence

```
uv run pytest -q      # 243 passed
uv run ruff check .   # All checks passed!
uv run mypy src       # Success: no issues found in 23 source files
```

SPEC gate — "add column + change type on a large table: plan flags the type change as
high-lock-risk with reasoning and suggests the safe multi-step pattern". On the live
1.2M-row `orders`:

```
plan_id=beRpo3j8CtAxNKpr atomic=True highest_risk=high dry_run_ok=True

  ALTER TABLE "orders" ADD COLUMN "note" text
    op=metadata_only  risk=low   estimate=1ms     confidence=high
    why: PG11+ does not rewrite the table for a non-volatile default...

  ALTER TABLE "orders" ALTER COLUMN "total_cents" TYPE bigint
    op=table_rewrite  risk=high  estimate=4800ms  confidence=medium
    why: rewrites every row and rebuilds every index, holding AccessExclusiveLock...
    SAFER: Add a new column of the target type, backfill in batches, sync with a
           trigger, swap the names, then drop the old column.
```

Apply / ledger / gating verified live: safe change applied and recorded; a 7-step
destructive plan refused with `CONFIRMATION_REQUIRED` naming the actual data loss
(`orders.customer_id holds data that a down-migration cannot restore`); 1,200,000 rows
untouched after refusal; ledger history clean with `in_flight: []`.

---

## 2026-08-24 · Phase 3 — Explain & performance brain

- **PHASE-3:** `plan_analysis.py` — pure functions over `EXPLAIN (FORMAT JSON)` output,
  no DB access, so every rule is unit-testable against captured plans. Six verdict
  kinds: `seq_scan_large_table`, `expensive_filter`, `estimate_divergence`,
  `sort_spill`, `nested_loop_blowup`, `dominant_node`.
- **PHASE-3:** Two correctness details that decide whether the output is trustworthy:
  - **Loops.** `Actual Rows` / `Actual Total Time` / `Plan Rows` are all *per loop*.
    A node reporting `Actual Rows: 80000, Actual Loops: 3` returned 240,000 rows. A
    parser that compares `Plan Rows` to `Actual Rows` directly reports wild estimate
    divergence on every parallel plan in existence.
  - **Parallel loops ≠ sequential loops.** Got this wrong first and the dev database
    caught it: the analyzer emitted *"5180ms of 2400ms total (216%)"*. Under a Nested
    Loop, `Actual Loops` counts iterations; under a Gather it counts concurrent
    *workers*. Rows sum across workers, wall-clock time does not. Nodes now carry a
    `parallel` flag set while descending through Gather/Gather Merge; time multiplies
    by loops only when it isn't set. Same query now reads 876ms of 1248ms (70%).
- **PHASE-3:** Self time, not total time, identifies the bottleneck — a node's
  `Actual Total Time` includes its children, so ranking by total always names the root.
- **PHASE-3:** Thresholds are named constants with stated reasoning (`SEQ_SCAN_MIN_ROWS
  = 10_000` — below that a seq scan is the *correct* plan and flagging it would train
  the reader to ignore the verdict list). Several tests assert healthy queries produce
  **no** verdict.
- **PHASE-3:** `tools/explain.py` — `query.explain`. The safety issue unique to this
  tool: **`EXPLAIN ANALYZE DELETE FROM orders` performs the delete.** `analyze=false`
  (default) never executes. `analyze=true` on a mutating statement runs inside a
  transaction that is *always* rolled back, and still goes through the full
  guardrail + confirmation-token + audit path — rollback is not a complete undo
  (`nextval()` and side effects inside functions don't roll back), so the gate stays
  rather than being waived on the strength of "we roll it back anyway". The rollback is
  structural: the transaction block is exited by raising, so no code path commits.
- **PHASE-3:** `tools/advisor.py` — `index.advise`. Findings ordered by confidence:
  unused indexes and redundant (prefix) indexes from catalog structure, sequential-scan
  hotspots and slowest statements from workload stats. Primary-key and unique indexes
  are never reported as unused or redundant — they enforce correctness, and "nobody
  scanned it" is not a reason to drop a uniqueness guarantee. Degrades gracefully with
  an explanatory note when `pg_stat_statements` isn't installed.
- **PHASE-3 · BUG found on the live stack:** the advisor recommended
  `DROP INDEX idx_orders_customer_id` — an index a query had used *seconds earlier*.
  `pg_stat` counters lag, so it read a stale `idx_scan = 0`. Confidently wrong advice
  is worse than none: it discredits every other finding. Now the observation window is
  measured from `pg_stat_database.stats_reset`, reported in the response, and gates the
  recommendation — under 7 days the finding is marked `confidence: low` and the
  suggestion becomes an explicit "do NOT drop this yet".

### Gate evidence

```
uv run pytest -q      # 163 passed
uv run ruff check .   # All checks passed!
uv run mypy src       # Success: no issues found in 17 source files
```

SPEC gate requires ≥10 seeded slow-query scenarios diagnosed correctly —
`tests/test_explain.py` has 18 against real Postgres (seq scan, expensive filter, sort
spill, nested loop, estimate divergence from correlated columns, plus negative cases:
indexed lookup, PK lookup, and small-table scan must produce *no* verdict).

Live dev stack (1.2M-row `orders`):

```
query.explain "SELECT * FROM orders WHERE status='paid' ORDER BY total_cents" analyze=true
  [warning] sort_spill           sort spilled to disk (external merge, 3,712 kB)
  [warning] seq_scan_large_table sequential scan examined 1,200,000 rows on orders
  [info]    dominant_node        876ms of 1248ms total (70%) in this node alone

index.advise
  hotspot orders: 21 sequential scans read 8,400,000 rows from a table of ~1,200,000
  stats_window: {'observed_for': 'unknown', 'sufficient_for_unused_index_advice': False}
```

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

- [ ] Phase 5: Docker environment layer — `env.topology`, `container.logs/stats`,
      correlation hints; `container.restart/exec` double-gated behind `--approval-mode`
- [ ] `migration.rollback` — the ledger stores the applied steps, but generated
      down-migrations are not implemented yet. Deliberately deferred rather than
      half-built: a rollback that silently loses data is worse than none, so it needs
      the honest-refusal path (PRD FR-3) done properly.
- [ ] Known gap to close: classifier can't see writes inside volatile functions
      (`SELECT my_func()`); needs a `pg_proc.provolatile` catalog lookup. Currently
      caught only by the read-only pool at execution time (ADR-001).
- [ ] `index.advise` names the table taking sequential scans but not the *column* to
      index — that needs plan inspection per statement. Currently honest about it
      ("run query.explain on this statement") rather than fabricating a CREATE INDEX.
