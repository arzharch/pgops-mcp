# Module layout. âœ… = exists, others are created when their phase starts.

src/pgops/
â”œâ”€â”€ __init__.py          âœ…
â”œâ”€â”€ __main__.py          âœ… entry: args, logging (stderr!), server wiring, transport
â”œâ”€â”€ config.py            âœ… DSN, flags, audit path, timeout/row/pool tiers
â”œâ”€â”€ connections.py       âœ… ConnectionManager: readonly + readwrite pools, bounded acquire
â”œâ”€â”€ classifier.py        âœ… SQL classification (deny-by-default) â€” ADR-001
â”œâ”€â”€ guardrails.py        âœ… unbounded-mutation detection, confirmation tokens
â”œâ”€â”€ audit.py             âœ… append-only JSONL audit log
â”œâ”€â”€ plan_analysis.py     âœ… EXPLAIN plan tree + verdict rules (pure, no DB)
â”œâ”€â”€ serialize.py         âœ… asyncpg value â†’ JSON-safe conversion
â”œâ”€â”€ timing.py            âœ… perf_counter-based elapsed measurement (monotonic is
â”‚                           15.6ms-resolution on Windows and reported 0.0)
â”œâ”€â”€ errors.py            âœ… structured error codes + tool_boundary decorator
â”œâ”€â”€ py.typed             âœ…
â”œâ”€â”€ tools/
â”‚   â”œâ”€â”€ schema.py        âœ… schema.inspect  (schema.diff lands in Phase 4)
â”‚   â”œâ”€â”€ query.py         âœ… query.read
â”‚   â”œâ”€â”€ write.py         âœ… query.write
â”‚   â”œâ”€â”€ explain.py       âœ… query.explain
â”‚   â”œâ”€â”€ advisor.py       âœ… index.advise
â”‚   â”œâ”€â”€ health.py        âœ… db.health
â”‚   â”œâ”€â”€ migrations.py    âœ… migration.plan / apply / history  (rollback still open)
â”‚   â””â”€â”€ environment.py   âœ… env.topology/correlate, container.logs/stats/restart/exec
â””â”€â”€ migrations/
    â”œâ”€â”€ diff.py          âœ… structural schema diff â†’ dependency-ordered change set
    â”œâ”€â”€ lock_analysis.py âœ… op-class Ã— table-size estimates + safe patterns (ADR-004)
    â””â”€â”€ ledger.py        âœ… pgops_migrations bookkeeping, checksums, crash recovery

tests/
â”œâ”€â”€ conftest.py          âœ… testcontainers session fixture + perf-sized seed data
â”œâ”€â”€ test_classifier.py   âœ…  test_guardrails.py    âœ…  test_audit.py       âœ…
â”œâ”€â”€ test_connections.py  âœ…  test_config.py        âœ…  test_tool_boundary.py âœ…
â”œâ”€â”€ test_query_read.py   âœ…  test_query_write.py   âœ…  test_schema_inspect.py âœ…
â”œâ”€â”€ test_health.py       âœ…  test_plan_analysis.py âœ…  test_explain.py     âœ…
â”œâ”€â”€ test_advisor.py      âœ…  test_server.py        âœ… (end-to-end via FastMCP)
â”œâ”€â”€ test_lock_analysis.py âœ… test_diff.py          âœ…  test_migrations.py âœ…
â”œâ”€â”€ test_timing.py       âœ…  test_environment.py   âœ… (skips without a Docker daemon)
â””â”€â”€ test_stdio_server.py âœ… (server as a real subprocess over stdio)

Deviations from the original plan, and why:
- `tools/query.py` split into `query.py` (read) + `write.py` (write). The write path
  carries guardrails, tokens and audit; keeping both in one file made the read path
  harder to review, and the read path is the one that must be obviously safe.
- Plan parsing lives in `plan_analysis.py`, not inside `tools/explain.py`, so the
  verdict rules are pure functions unit-testable against captured plan JSON with no
  database involved.
- No `tests/fixtures/sql/` â€” scenario data is seeded by the `perf_dsn` fixture in
  conftest.py instead, which keeps each scenario's setup next to its assertions.
