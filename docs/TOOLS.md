# TOOLS — Full Tool Catalog

**Status:** v1.0 (target) · Phase mapping in [SPEC.md](SPEC.md)

Conventions:
- All tools return structured JSON. Errors are objects: `{"error": {"code", "message", "hint"}}`.
- Destructive tools require `confirm_token` obtained from a prior refusal response.
- SQL in audit logs is stored alongside its SHA-256 hash.
- Tool names below are the exact registered names — they are a public contract and do
  not track the Python function names behind them.
- No tool ever raises to the client: unexpected failures are caught at the tool boundary
  and returned as `{"error": {"code": "INTERNAL_ERROR", ...}}` with the traceback sent
  to the server's stderr log instead.

---

## schema.inspect
**Phase 1 ✅ implemented · Role: readonly**

Inspect database structure.

| Param | Type | Default | Notes |
|---|---|---|---|
| level | enum: summary\|tables\|full | summary | full includes constraints, indexes, extensions |
| table | str? | null | scope to one table |

Returns: tables with columns/types/nullability, PK/FKs, indexes, per-table size estimates,
extension list, schema-level stats.

---

## query.read
**Phase 1 ✅ implemented · Role: readonly**

Execute a read-only statement.

| Param | Type | Default | Notes |
|---|---|---|---|
| sql | str | required | SELECT / WITH / EXPLAIN only |
| limit | int | 100 | max rows returned; hard cap 10,000 |
| timeout_ms | int | 5000 | tiered cap, server max overrides |

Guardrails: classifier must classify as read; else refused with hint to use query.write.
Values serialized safely (JSONB, arrays, numerics).

---

## query.write
**Phase 2 ✅ implemented · Role: readwrite + confirmation for destructive**

Execute a mutating statement. Not registered at all when the server runs with
`--read-only`.

| Param | Type | Default | Notes |
|---|---|---|---|
| sql | str | required | INSERT/UPDATE/DELETE/DDL |
| confirm_token | str? | null | required iff refused on a prior call |
| timeout_ms | int? | 5000 | tiered cap, server max overrides |

Behavior:
- INSERT / bounded UPDATE/DELETE → executes immediately
- UPDATE/DELETE without WHERE → blocked, returns reason + confirm token
- DROP/TRUNCATE/ALTER ... DROP COLUMN → always destructive class
- Unclassifiable statements (`VACUUM`, `DO $$...$$`, multi-statement) → treated as
  destructive per ADR-001
- Response: `{rows_affected, duration_ms, audit_id, classification}`

Confirmation tokens are single-use, expire after 5 minutes, and are bound to the
SHA-256 of the exact statement they were issued for — redeeming one against different
SQL fails with `CONFIRMATION_MISMATCH` and does not consume the token.

Every call is audited, including refusals.

Error codes: `CONFIRMATION_REQUIRED`, `INVALID_CONFIRMATION`, `CONFIRMATION_MISMATCH`,
`READ_ONLY_MODE`, `QUERY_TIMEOUT`, `INVALID_ARGUMENT`, `POOL_EXHAUSTED`.

---

## query.explain
**Phase 3 ✅ implemented · Role: readonly, or readwrite when `analyze=true` on a write**

Explain a statement and return a parsed verdict.

| Param | Type | Default | Notes |
|---|---|---|---|
| sql | str | required | any single statement |
| analyze | bool | false | **executes the statement** — see below |
| confirm_token | str? | null | required for `analyze=true` on a guarded statement |
| timeout_ms | int? | 5000 | tiered cap, server max overrides |

⚠️ `EXPLAIN ANALYZE DELETE FROM t` **performs the delete** — `ANALYZE` executes the
statement. Handling:
- `analyze=false` (default): plans only, never executes. Safe for any statement.
- `analyze=true` on a read: executes a read, readonly pool.
- `analyze=true` on a mutating statement: runs inside an always-rolled-back transaction
  on the readwrite pool, gated by the same guardrails and confirmation token as
  `query.write`, and audited. Rollback does not undo `nextval()` or side effects inside
  called functions, which is why the gate is not waived.

Returns `{analyzed, plan, verdicts[], planning_time_ms?, execution_time_ms?}`.
`plan` is a compact tree (`node`, `planned_rows`, `actual_rows`, `total_time_ms`,
`self_time_ms`, `loops`, `children`) — the raw plan's per-worker buffer counters are
dropped as noise.

Each verdict is `{kind, severity, node, evidence, suggestion}`, sorted most-severe
first. Kinds: `seq_scan_large_table`, `expensive_filter`, `estimate_divergence`,
`sort_spill`, `nested_loop_blowup`, `dominant_node`.

Row and time values account for `Actual Loops`, and distinguish parallel workers
(concurrent — times overlap) from nested-loop iterations (sequential — times add).

---

## index.advise
**Phase 3 ✅ implemented · Role: readonly**

| Param | Type | Default | Notes |
|---|---|---|---|
| limit | int | 10 | how many slow statements to return |

Returns:
- `stats_window` — how long the counters have been accumulating, and whether that is
  long enough to trust unused-index findings
- `unused_indexes` — zero scans, with `confidence` (`high` only when statistics span
  ≥7 days) and size cost. Primary-key and unique indexes are never reported.
- `redundant_indexes` — column list is a strict prefix of another index's. Unique and
  primary-key indexes are excluded: a composite serves the same queries but does not
  enforce the same constraint.
- `scan_hotspots` — tables taking sequential scans under load. Names the table, not the
  column: identifying the column needs plan inspection, so the suggestion is to run
  `query.explain` rather than a fabricated `CREATE INDEX`.
- `top_statements` — slowest by total execution time (requires `pg_stat_statements`;
  absence degrades to catalog findings plus an explanatory note)

---

## db.health
**Phase 1 ✅ implemented · Role: readonly**

Health snapshot: connection counts by state, cache hit ratio, dead tuples/top bloat
tables, long-running queries (>5s), blocked sessions via `pg_blocking_pids()`. Each
finding carries `{category, severity, summary, detail}` where severity is one of
`ok | info | warning | critical`.

---

## migration.plan
**Phase 4 ✅ implemented · Role: readonly (dry-run is rolled back)**

Diff the live schema against a target and render an annotated plan. Executes nothing.

| Param | Type | Default | Notes |
|---|---|---|---|
| target | object | required | desired state (see below) |
| allow_drops | bool | false | required before any removal is emitted |
| dry_run | bool | true | executes steps in an always-rolled-back transaction |

Target format — desired *state*, not migration SQL:

```json
{"tables": {"orders": {
   "columns":     {"note": {"type": "text", "nullable": true, "default": "'x'"}},
   "indexes":     {"idx_orders_status": "status"},
   "constraints": {"orders_total_chk": "CHECK (total_cents >= 0)"}
}}}
```

Returns `{plan_id, checksum, atomic, highest_risk, destructive, steps[], dry_run_ok,
notes[]}`. Each step carries its SQL plus `lock_impact`:
`{operation, risk, lock_mode, blocks_reads, blocks_writes, rewrites_table, estimate_ms,
confidence, reasoning, safe_alternative, transactional}`.

Behaviour worth knowing:
- Tables/columns absent from the target are **left alone** unless `allow_drops=true`; a
  note lists what was skipped.
- Ordering is dependency-safe: creations tables → columns → constraints → indexes, drops
  in reverse.
- Type aliases are normalized (`int`/`integer`, `varchar`/`character varying`) so
  identical types never produce a spurious rewriting `ALTER TYPE`.
- `atomic: false` when the plan contains `CREATE INDEX CONCURRENTLY`, which cannot run
  in a transaction — a later failure then leaves earlier steps applied.
- `pgops_migrations` is pgops's own ledger: excluded from diffs, refused as a target.
- Unsupported constructs (views, triggers, functions, partitions) are **refused**, not
  silently skipped.

---

## migration.apply
**Phase 4 ✅ implemented · Role: readwrite + confirmation**

| Param | Type | Default | Notes |
|---|---|---|---|
| plan_id | str | required | from a prior migration.plan |
| confirm_token | str? | null | required for destructive or high-risk plans |
| name | str | "unnamed" | recorded in the ledger |

Destructive steps or any `high` risk step are refused on the first call with a reason
naming the actual data loss. Plans are held in memory for the life of the server.
Re-applying an already-applied plan is a no-op.

Ledger (`pgops_migrations`): `migration_id, name, checksum, status, started_at,
finished_at, duration_ms, applied_by, error, steps`. The row is written `in_flight`
**before** the DDL runs, so an interrupted migration is detectable; a stranded
`in_flight` row causes later applies to refuse with `MIGRATION_IN_FLIGHT` rather than
guessing which steps landed.

Error codes: `CONFIRMATION_REQUIRED`, `CONFIRMATION_MISMATCH`, `MIGRATION_IN_FLIGHT`,
`MIGRATION_FAILED`, `READ_ONLY_MODE`, `INVALID_ARGUMENT`.

---

## migration.history
**Phase 4 ✅ implemented · Role: readonly**

Ledger history plus any `in_flight` migrations, with a `warning` when one is present.

---

## migration.rollback
**Phase 4 · not yet implemented**

Deferred deliberately. FR-3 requires generating a down-migration *and refusing with an
explanation when it would lose data irrecoverably*; a rollback that silently drops what
it cannot restore is worse than none. The ledger already stores the applied steps.

---

## env.topology
**Phase 5 · Docker read-only API**

Discover containers/images/ports/volumes; identify Postgres container matching our DSN;
group by compose project. No daemon writes.

## container.logs
**Phase 5** Tail logs with severity filter and time bound.

## container.stats
**Phase 5** CPU/mem/IO snapshot per container; correlation hints vs db.health findings.

## container.restart / container.exec
**Phase 5 · Gated twice**: server must run with `--approval-mode`, AND call needs
confirm_token. Refuse otherwise with explanation.
