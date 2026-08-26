# Module layout

Every module below exists. Where a name is not self-explanatory, the note says what the
module is *for* rather than what it contains.

```
src/pgops/
├── __init__.py
├── __main__.py          entry: args, logging (stderr!), server wiring, transports, CLI
├── config.py            DSN, flags, audit path, timeout/row/pool tiers
├── connections.py       ConnectionManager: readonly + readwrite pools, bounded acquire
├── classifier.py        SQL classification (deny-by-default) — ADR-001
├── function_safety.py   pg_proc.provolatile lookup — catches writes hidden in functions
├── guardrails.py        unbounded-mutation detection, confirmation tokens
├── approval.py          elicitation (ask the user directly), tokens as the fallback
├── auth.py              RS256 agent tokens, scopes, keygen — HTTP transport only
├── middleware.py        per-tool scope enforcement + caller identity
├── audit.py             append-only JSONL audit log, with the calling actor
├── replay.py            re-classify (or re-execute) an audit log — forensic + regression
├── observability.py     OTel spans/metrics, /health and /ready — all optional, all no-op
│                        by default
├── sampling.py          asks the *client's* model; this server ships no API key
├── completions.py       table-name autocomplete for pgops://schema/{table}
├── resources.py         read-only state addressable by URI
├── prompts.py           user-invoked workflows — the tool *ordering* no single tool encodes
├── plan_analysis.py     EXPLAIN plan tree + verdict rules (pure, no DB)
├── serialize.py         asyncpg value → JSON-safe conversion
├── timing.py            perf_counter-based elapsed measurement (monotonic is
│                        15.6ms-resolution on Windows and reported 0.0)
├── errors.py            structured error codes + tool_boundary decorator
├── py.typed
├── tools/
│   ├── schema.py        schema.inspect
│   ├── query.py         query.read
│   ├── write.py         query.write
│   ├── explain.py       query.explain (+ optional prose summary via sampling)
│   ├── advisor.py       index.advise
│   ├── health.py        db.health
│   ├── migrations.py    migration.plan / describe / apply / rollback / history
│   └── environment.py   env.topology/correlate, container.logs/stats/restart/exec
└── migrations/
    ├── diff.py          structural schema diff → dependency-ordered change set
    ├── lock_analysis.py op-class × table-size estimates + safe patterns (ADR-004)
    ├── rollback.py      reversibility analysis — refuses rather than half-undoing
    └── ledger.py        pgops_migrations bookkeeping, checksums, crash recovery
```

## Distribution

```
pyproject.toml           an application, not a library — no import surface is promised
server.json              MCP Registry manifest: pypi + oci package entries
Dockerfile               two-stage, non-root, audit log on a declared volume
.github/workflows/
├── ci.yml               lint, types, test suite; live eval + benchmarks as artifacts
└── publish.yml          tag-gated: verify → (pypi, container) → registry
```

## Tests

```
tests/
├── conftest.py             testcontainers session fixture + perf-sized seed data
├── foundations             test_bootstrap, test_config, test_timing, test_tool_boundary
├── safety core             test_classifier, test_guardrails, test_function_safety,
│                           test_properties (Hypothesis, attacks the invariant itself)
├── data path               test_connections, test_query_read, test_query_write,
│                           test_schema_inspect, test_health, test_audit,
│                           test_audit_identity
├── performance brain       test_plan_analysis, test_explain, test_advisor
├── migrations              test_lock_analysis, test_diff, test_migrations, test_rollback
├── MCP surface             test_resources_prompts, test_approval, test_sampling,
│                           test_completions, test_server, test_stdio_server
├── auth                    test_auth, test_scope_enforcement
├── ops                     test_observability, test_replay, test_environment
│                           (test_environment skips without a Docker daemon)
└── adversarial             test_redteam, test_live_server — marked `live`, boot a real
                            HTTP server; excluded from the default run
```

## Deviations from the original plan, and why

- `tools/query.py` split into `query.py` (read) + `write.py` (write). The write path
  carries guardrails, tokens and audit; keeping both in one file made the read path
  harder to review, and the read path is the one that must be obviously safe.
- Plan parsing lives in `plan_analysis.py`, not inside `tools/explain.py`, so the verdict
  rules are pure functions unit-testable against captured plan JSON with no database.
- Scope enforcement lives in `middleware.py`, not in each tool. A cross-cutting rule
  applied per call site is a rule that will eventually be missed — which is exactly how
  `TOOL_SCOPES` came to be documentation rather than enforcement before this existed.
- Reversibility analysis lives in `migrations/rollback.py` as pure functions over
  recorded steps, separate from the tool that executes a rollback, so the interesting
  judgement (what *cannot* be undone) is testable without a database.
- No `tests/fixtures/sql/` — scenario data is seeded by the `perf_dsn` fixture in
  conftest.py instead, which keeps each scenario's setup next to its assertions.
