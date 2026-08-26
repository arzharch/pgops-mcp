# Module layout. ✅ = exists, others are created when their phase starts.

src/pgops/
├── __init__.py          ✅
├── __main__.py          ✅ entry: args, logging (stderr!), server wiring, transport
├── config.py            ✅ DSN, flags, audit path, timeout/row/pool tiers
├── connections.py       ✅ ConnectionManager: readonly + readwrite pools, bounded acquire
├── classifier.py        ✅ SQL classification (deny-by-default) — ADR-001
├── guardrails.py        ✅ unbounded-mutation detection, confirmation tokens
├── audit.py             ✅ append-only JSONL audit log
├── plan_analysis.py     ✅ EXPLAIN plan tree + verdict rules (pure, no DB)
├── serialize.py         ✅ asyncpg value → JSON-safe conversion
├── timing.py            ✅ perf_counter-based elapsed measurement (monotonic is
│                           15.6ms-resolution on Windows and reported 0.0)
├── errors.py            ✅ structured error codes + tool_boundary decorator
├── py.typed             ✅
├── tools/
│   ├── schema.py        ✅ schema.inspect  (schema.diff lands in Phase 4)
│   ├── query.py         ✅ query.read
│   ├── write.py         ✅ query.write
│   ├── explain.py       ✅ query.explain
│   ├── advisor.py       ✅ index.advise
│   ├── health.py        ✅ db.health
│   ├── migrations.py    ✅ migration.plan / apply / history  (rollback still open)
│   └── environment.py   ✅ env.topology/correlate, container.logs/stats/restart/exec
└── migrations/
    ├── diff.py          ✅ structural schema diff → dependency-ordered change set
    ├── lock_analysis.py ✅ op-class × table-size estimates + safe patterns (ADR-004)
    └── ledger.py        ✅ pgops_migrations bookkeeping, checksums, crash recovery

tests/
├── conftest.py          ✅ testcontainers session fixture + perf-sized seed data
├── test_classifier.py   ✅  test_guardrails.py    ✅  test_audit.py       ✅
├── test_connections.py  ✅  test_config.py        ✅  test_tool_boundary.py ✅
├── test_query_read.py   ✅  test_query_write.py   ✅  test_schema_inspect.py ✅
├── test_health.py       ✅  test_plan_analysis.py ✅  test_explain.py     ✅
├── test_advisor.py      ✅  test_server.py        ✅ (end-to-end via FastMCP)
├── test_lock_analysis.py ✅ test_diff.py          ✅  test_migrations.py ✅
├── test_timing.py       ✅  test_environment.py   ✅ (skips without a Docker daemon)
└── test_stdio_server.py ✅ (server as a real subprocess over stdio)

Deviations from the original plan, and why:
- `tools/query.py` split into `query.py` (read) + `write.py` (write). The write path
  carries guardrails, tokens and audit; keeping both in one file made the read path
  harder to review, and the read path is the one that must be obviously safe.
- Plan parsing lives in `plan_analysis.py`, not inside `tools/explain.py`, so the
  verdict rules are pure functions unit-testable against captured plan JSON with no
  database involved.
- No `tests/fixtures/sql/` — scenario data is seeded by the `perf_dsn` fixture in
  conftest.py instead, which keeps each scenario's setup next to its assertions.
