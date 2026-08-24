# Planned module layout (Phase 1+). Create each file when its phase starts.

src/pgops/
├── __init__.py
├── __main__.py          # entry: args, server wiring, transport
├── config.py            # DSN, flags: --read-only, --approval-mode, audit path, timeouts
├── connections.py       # ConnectionManager: readonly + readwrite asyncpg pools, timeout tiers
├── classifier.py        # SQL classification (deny-by-default) — ADR-001
├── guardrails.py        # row limits, unbounded-mutation detection, confirmation tokens
├── audit.py             # append-only JSONL audit log
├── tools/
│   ├── schema.py        # schema.inspect, schema.diff
│   ├── query.py         # query.read, query.write
│   ├── explain.py       # query.explain + plan parser + verdicts
│   ├── advisor.py       # index.advise
│   ├── health.py        # db.health
│   ├── migrations.py    # migration.plan / apply / rollback + ledger
│   └── environment.py   # env.topology, container.logs/stats/restart/exec
├── migrations/
│   ├── diff.py          # structural schema diff → ordered change set
│   ├── lock_analysis.py # op-class × table-size estimates + safe-pattern rewrites (ADR-004)
│   └── ledger.py        # pgops_migrations bookkeeping, checksums, crash recovery
└── errors.py            # structured error codes

tests/
├── fixtures/sql/        # seeded slow queries, big-table scenarios, migration cases
├── conftest.py          # testcontainers Postgres session fixture
├── test_classifier.py
├── test_guardrails.py
├── test_explain.py
└── test_migrations.py
