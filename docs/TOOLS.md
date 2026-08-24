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
**Phase 4 ✅ implemented · Role: readwrite + confirmation**

| Param | Type | Default | Notes |
|---|---|---|---|
| ledger_id | int | required | the ledger row id, from migration.history |
| confirm_token | str? | null | required before anything executes |

Reverses an applied migration by inverting its recorded steps, last one first (the
index must drop before the table it depends on). Each recorded step is classified into
one of three honest outcomes:

- **reversible** — e.g. `CREATE INDEX` → `DROP INDEX`; an index is derived data
- **reversible with data loss** — e.g. `ADD COLUMN` → `DROP COLUMN`: the schema reverts,
  but every value written to that column since is destroyed. The confirmation reason
  says so explicitly.
- **irreversible** — `DROP COLUMN`, `DROP TABLE`, type changes without a recorded
  previous type

**Any irreversible step refuses the whole rollback** and issues *no* token: unlike a
risky-but-possible rollback, no human answer changes what is possible. The refusal names
the offending step and points at restore-from-backup as the only real way back.

Also refused:
- migrations applied **after** this one (rolling back underneath them could break their
  assumptions) — roll back the later ones first
- rows whose status is not `applied`
- migrations recorded without structured step data (pre-structured-recording entries)

On success the ledger row becomes `rolled_back` — history stays honest without claiming
destroyed data came back.

Error codes: `CONFIRMATION_REQUIRED`, `CONFIRMATION_MISMATCH`,
`MIGRATION_IRREVERSIBLE`, `MIGRATION_IN_FLIGHT` (stacked), `MIGRATION_FAILED`,
`INVALID_ARGUMENT`.

---

## env.topology
**Phase 5 ✅ implemented · Docker read-only API**

| Param | Type | Default | Notes |
|---|---|---|---|
| all_containers | bool | false | include stopped containers |

Returns `{dsn_host_port, database_container, database_container_note, containers[],
compose_projects{}}`. The database container is matched by **published host port** from
the DSN, not by image name — a machine commonly runs several Postgres containers, and
matching on the image picks the wrong one.

**Container environment variables are never returned.** `Config.Env` holds
`POSTGRES_PASSWORD` and every other secret on the box; this tool returns an explicit
allowlist of fields (name, image, status, health, compose project/service, ports, mount
*destinations*) rather than filtering a denylist, so a new Docker API field cannot leak
by default.

## container.logs
**Phase 5 ✅ implemented**

| Param | Type | Default | Notes |
|---|---|---|---|
| name | str | required | container name |
| tail | int | 100 | capped at 2000 |
| min_severity | str? | null | DEBUG/INFO/NOTICE/LOG/WARNING/ERROR/FATAL/PANIC |
| since_seconds | int? | null | time bound |

Returns `{container, lines[], returned, scanned, min_severity}`. Lines with no
recognizable severity are dropped when filtering.

## container.stats
**Phase 5 ✅ implemented**

CPU percent, memory (used/limit/percent), total IO bytes, and CPU throttling counters.
Takes ~1s: a CPU percentage needs two samples to compute a delta. Memory matches
`docker stats` accounting (usage minus reclaimable `inactive_file`).

## env.correlate
**Phase 5 ✅ implemented**

Runs `db.health`, finds the database container, and returns plain-language hints joining
database symptoms to container resource pressure. Hints are phrased "consistent with",
never as diagnoses, and the tool stays quiet when nothing is wrong.

## container.restart / container.exec
**Phase 5 ✅ implemented · Gated twice (exec: three times)**

Not registered as tools at all unless the server runs with `--approval-mode`
(`PGOPS_APPROVAL_MODE=true`) — an agent is never told they exist. With the flag, each
call still requires a `confirm_token` bound to that specific container and command.

`container.exec` additionally enforces a **read-only diagnostic command allowlist**
(`ps`, `df`, `free`, `uptime`, `cat`, `ls`, `pg_isready`, `psql`, …), checked by
basename so `/bin/bash` cannot bypass it. It deliberately does not offer an arbitrary
shell.

Error codes: `APPROVAL_MODE_REQUIRED`, `EXEC_NOT_ALLOWED`, `CONFIRMATION_REQUIRED`,
`CONFIRMATION_MISMATCH`, `CONTAINER_NOT_FOUND`, `DOCKER_UNAVAILABLE`.

---

# Resources

Read-only state addressable by URI. A client can attach these as context without the
model spending a turn on a tool call. All mirror data already reachable through tools.

| URI | Contents |
|---|---|
| `pgops://schema` | Full schema: tables, columns, constraints, indexes, extensions |
| `pgops://schema/summary` | Table names, row estimates, sizes — the cheap version |
| `pgops://schema/{table}` | One table's full definition (template) |
| `pgops://health` | Health snapshot with severities |
| `pgops://migrations` | Ledger history, including interrupted migrations |
| `pgops://audit/recent` | Recent audit metadata — **SQL text omitted** (see below) |
| `pgops://config` | Effective safety configuration — **DSN omitted** |

Two deliberate redactions:
- `pgops://config` never includes the DSN, which carries the password.
- `pgops://audit/recent` returns verdicts, timings, tool names and SQL *hashes* but not
  statement text. The on-disk log keeps full SQL because an incident review needs it; a
  resource may be auto-attached to model context, and executed SQL embeds literal values
  (an email in a WHERE clause, an amount in an UPDATE).

---

# Prompts

| Name | Arguments | Purpose |
|---|---|---|
| `diagnose-slow-query` | `sql` | Evidence-driven slow query investigation |
| `plan-safe-migration` | `description` | Zero-downtime schema change, lock impact first |
| `incident-triage` | — | Cheapest, most-likely checks first |
| `review-index-health` | — | Index review respecting the statistics window |
| `explain-safety-model` | — | What this server will and will not permit |

Prompts encode the *order* to use tools in and what to do with the answers — the part no
individual tool can express.

---

# Authentication

stdio requires none: the server is a subprocess the client spawns, with no open port and
no remote caller. HTTP requires it and **refuses to start without `--public-key`**.

```bash
pgops-mcp keygen                                     # RS256 keypair
pgops-mcp issue-token --subject my-agent             # read-only by default
pgops-mcp issue-token --subject bot --scope pgops:read --scope pgops:write
pgops-mcp scopes                                     # scope required by each tool
pgops-mcp --transport http --public-key <path>       # binds 127.0.0.1 by default
```

| Scope | Grants |
|---|---|
| `pgops:read` | schema.inspect, query.read, query.explain, db.health, index.advise, migration.plan, migration.history, env.*, container.logs/stats |
| `pgops:write` | query.write, migration.apply, migration.rollback |
| `pgops:admin` | container.restart, container.exec |

The server holds only the public key — it can verify tokens, never issue them. A tool
with no scope entry requires `pgops:admin` (deny by default). `subject` identifies the
agent for audit purposes.

---

# Human approval

Dangerous actions ask the **user** directly via MCP elicitation where the client supports
it, rather than routing approval through the agent. Where it does not, the server falls
back to the confirmation-token protocol — degraded, but never to "allowed". A user who
declines gets `CONFIRMATION_DECLINED` and **no token**: an explicit refusal is not
convertible into a credential. The audit log records which method approved each action.
