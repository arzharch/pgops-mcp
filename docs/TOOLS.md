# TOOLS — Full Tool Catalog

**Status:** v1.0 (target) · Phase mapping in [SPEC.md](SPEC.md)

Conventions:
- All tools return structured JSON. Errors are objects: `{"error": {"code", "message", "hint"}}`.
- Destructive tools require `confirm_token` obtained from a prior refusal response.
- SQL in audit logs is stored alongside its SHA-256 hash.

---

## schema.inspect
**Phase 1 · Role: readonly**

Inspect database structure.

| Param | Type | Default | Notes |
|---|---|---|---|
| level | enum: summary\|tables\|full | summary | full includes constraints, indexes, extensions |
| table | str? | null | scope to one table |

Returns: tables with columns/types/nullability, PK/FKs, indexes, per-table size estimates,
extension list, schema-level stats.

---

## query.read
**Phase 1 · Role: readonly**

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
**Phase 2 · Role: readwrite + confirmation for destructive**

Execute a mutating statement.

| Param | Type | Default | Notes |
|---|---|---|---|
| sql | str | required | INSERT/UPDATE/DELETE/DDL |
| confirm_token | str? | null | required iff classified destructive |

Behavior:
- INSERT / bounded UPDATE/DELETE → executes immediately
- UPDATE/DELETE without WHERE, or WHERE on non-indexed column of large table → blocked,
  returns reason + confirm token
- DROP/TRUNCATE/ALTER ... DROP COLUMN → always destructive class
- Response: rows affected, duration, audit id

---

## query.explain
**Phase 3 · Role: readonly**

Explain a statement and return a parsed verdict.

| Param | Type | Default | Notes |
|---|---|---|---|
| sql | str | required | SELECT/INSERT/UPDATE/DELETE plans supported |
| analyze | bool | false | actually executes (on readwrite pool if mutating) |
| format | enum: json | json | text format not offered to agents |

Returns: plan tree (compact), plus `verdicts[]`: each `{kind, severity, node, evidence,
suggestion}` — kinds include seq_scan_large_table, estimate_divergence, sort_spill,
nested_loop_blowup, expensive_filter.

---

## index.advise
**Phase 3 · Role: readonly**

Index recommendations from workload statistics.

Returns:
- top slow statements (from pg_stat_statements)
- missing-index suggestions: columns/ordering, predicted impact, CREATE INDEX CONCURRENTLY DDL
- unused indexes (zero scans, size cost)
- redundant indexes (prefix of another)

---

## db.health
**Phase 1 · Role: readonly**

Health snapshot: connection counts by state, cache hit ratio, dead tuples/top bloat
tables, long-running queries, waiting locks, WAL/stats age. Each finding carries
severity + plain-language explanation.

---

## schema.diff
**Phase 4 · Role: readonly**

Diff live schema against a target definition or migration history point.

| Param | Type | Notes |
|---|---|---|
| target | object | desired schema subset (tables/columns/constraints/indexes) |
| base | str? | defaults to current live state |

Returns ordered change set with dependency-aware ordering.

---

## migration.plan
**Phase 4 · Role: readonly (no execution)**

Render a migration plan from a diff.

Returns steps: `[{id, sql, lock_impact: {estimate_ms, confidence, reasoning},
safe_pattern_applied?, dry_run_result}]`. Dry-run validates inside rolled-back
transactions where the step permits. Nothing is applied.

---

## migration.apply / migration.rollback
**Phase 4 · Role: readwrite + confirmation**

Apply or roll back a plan. Versioned ledger (`pgops_migrations`): id, checksum,
applied_at, duration, applied_by. Rollback uses generated down-migration; refuses with
explanation when it would lose data irrecoverably.

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
