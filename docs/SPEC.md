# SPEC — pgops-mcp Technical Specification

**Status:** v1.0 · Companion to [PRD.md](PRD.md) · **Last updated:** 2026-08-24

Rules: each phase ends with a **gate** (runnable command + observable outcome). No
phase-N work before the phase-(N-1) gate passes. Every deviation from this spec needs
an ADR. Docs drift = bug: flow.md updates in the same commit as any feature.

Stack: Python 3.12+, FastMCP, asyncpg, docker SDK (`docker`), pytest + testcontainers,
uv for env/package management. Transport: stdio first; Streamable HTTP later.

---

## Phase 0 — Project bootstrap (day 1)

- uv project skeleton, src layout, ruff+mypy+pytest wired, CI skeleton (GitHub Actions)
- Docker-compose dev environment: Postgres 16 + pg_stat_statements enabled + a seeded
  demo app schema (orders/customers/products with realistic row counts)
- `docs/` complete (this set)

**Gate ✅** `uv run pytest` passes trivial test; `docker compose up -d` yields a seeded DB.

---

## Phase 1 — Connection core + read path (week 1–2)

**Deliverables**
- `ConnectionManager`: two asyncpg pools per DSN — `readonly` (default) and `readwrite`
  (lazy). Statement timeout tiers. Pool health checks.
- `Classifier`: SQL statement classification (read / write / ddl / destructive /
  unknown). Deny-by-default: unrecognized → treated as most-dangerous class.
- Tools: `schema.inspect`, `query.read` (SELECT/WITH/EXPLAIN only, LIMIT enforcement,
  timeout), `db.health` (connections, cache hit ratio, dead tuples, long-running queries,
  locks).
- Structured errors: every failure returns machine-readable error codes, never raw dumps.

**Gate ✅**
```
uv run pgops-mcp --selfcheck        # connects, introspects, prints summary
# Claude Desktop: "show me the schema and how healthy is the database" works end-to-end
```
Tests: classifier table-driven cases; readonly role cannot write (proven); LIMIT enforced;
timeout kills runaway query.

---

## Phase 2 — Write path + safety architecture (week 3–4)

**Deliverables**
- `query.write`: classification → guardrail evaluation → execution. Unbounded
  UPDATE/DELETE (no WHERE or WHERE non-indexed on large table) blocked with reason.
- Confirmation protocol: destructive classes return a **confirmation token** (TTL 5 min);
  tool re-invocation with token executes. Token single-use.
- Audit log: append-only JSONL — ts, tool, sql hash + sql, duration, rows, verdict,
  client info. Configurable path.
- Approval mode flag for the server itself (`--allow-writes` default ON for write tool
  but destructive still token-gated; `--read-only` hard-disables write tools).

**Gate ✅**
```
uv run pytest tests/test_guardrails.py   # proves every guardrail blocks what it claims
# Claude Desktop: attempt DELETE without WHERE → blocked with explanation;
#                 confirm flow with token → executes → appears in audit log
```

---

## Phase 3 — Explain & performance brain (week 5–6)

**Deliverables**
- `query.explain`: EXPLAIN (ANALYZE opt-in, FORMAT JSON) → parsed plan tree → verdicts:
  seq-scan-on-large-table, bad join ordering, rows-estimate-vs-actual divergence,
  sort/hash spill to disk, expensive function scans.
- `index.advise`: reads pg_stat_statements → top offenders → missing-index suggestions
  (columns, orderings) with estimated impact; unused/redundant index detection.
- Verdict format shared with docs so agents can act on it deterministically.

**Gate ✅**
```
uv run pytest tests/test_explain.py       # ≥10 seeded slow-query scenarios diagnosed correctly
# Demo: seed a bad query → explain returns "seq scan on orders (2.1M rows),
#        missing index on (customer_id, created_at)" → advise proposes exactly that
```

---

## Phase 4 — Migration engine (week 7–9) ⭐ hardest phase

**Deliverables**
- `schema.diff`: structural diff of live schema vs. target (or vs. migration history)
  → ordered change set (tables → columns → constraints → indexes), dependency-aware.
- Lock-impact analysis per step: table size × operation class → estimate + confidence +
  plain-language reasoning. Known-safe patterns recognized (ADD COLUMN nullable,
  CREATE INDEX CONCURRENTLY, ADD CONSTRAINT NOT VALID + VALIDATE split).
- `migration.plan`: renders SQL steps + annotations; dry-run mode runs everything in a
  rolled-back transaction where possible to validate.
- `migration.apply` / `migration.rollback`: versioned ledger table (`pgops_migrations`),
  checksums, transactional apply where safe, down-migration generation with honest
  refusals when data loss would occur.

**Gate ✅**
```
uv run pytest tests/test_migrations.py
# Demo: add column + change type on 40M-row synthetic table:
#   plan flags the type change as high-lock-risk with reasoning and suggests
#   the safe multi-step pattern; apply works; rollback restores prior state
```

---

## Phase 5 — Docker environment layer (week 10–11)

**Deliverables**
- `env.topology`: docker SDK discovery — containers, images, ports, volumes, health;
  identify which container serves our DSN (match exposed port); compose project grouping.
- `container.logs` (severity-filtered tail), `container.stats` (CPU/mem/IO snapshot).
- Correlation hints: db.health findings joined with container stats ("shared_buffers
  pressure + container at 94% mem → likely under-provisioned").
- `container.restart`/`container.exec`: exist but refuse unless server started with
  `--approval-mode`; even then, confirmation-token gated.

**Gate ✅**
```
# On the dev compose stack: topology maps app→postgres correctly; logs/stats tools work;
# restart without approval-mode refuses; with approval-mode requires token
```

---

## Phase 6 — Packaging, distribution, polish (week 12–13)

**Deliverables**
- PyPI package (`pgops-mcp`) with console script; uv/pipx one-liner install
- Smithery manifest + official MCP registry submission
- README quickstart verified on clean machine; GIFs of Claude Desktop driving all flows
- Demo script + recorded walkthrough video
- Docs site-lite (mkdocs) if time permits

**Gate ✅** Acceptance criteria §7 of PRD all checked; v0.1 tagged and published.

---

## Cross-cutting rules

1. **Deny by default.** Anything unclassified is dangerous until proven otherwise.
2. **No raw error leakage.** All tool failures are structured error objects.
3. **Testcontainers everywhere.** Integration tests spin real Postgres; no mocks for
   guardrails — guards must be proven against the real engine.
4. **Every tool idempotent-or-honest.** Non-idempotent tools say so in their description.
5. **Docs-first changes.** Tool surface changes require TOOLS.md + PRD update in same PR.
