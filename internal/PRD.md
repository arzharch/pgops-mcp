# PRD — pgops-mcp

**Status:** v1.0 · **Owner:** Arsh Zakee Chowhan · **Last updated:** 2026-08-24

## 1. Problem statement

AI agents are increasingly asked to operate databases — "add a column", "why is this
query slow?", "clean up dead tuples" — but the tooling they get is either unsafe
(raw SQL execution with no guardrails) or shallow (introspection-only wrappers).
Meanwhile, in real deployments, the database lives inside a Docker environment whose
state (container health, resource pressure, logs) directly affects database behavior,
and no MCP server connects these worlds.

Three concrete failures today:
1. An agent runs `DELETE FROM orders` without a WHERE clause and there is no undo.
2. An agent generates an `ALTER TABLE` that takes an exclusive lock on a 40M-row table
   for six minutes in production. Nobody warned it.
3. An agent says "the DB seems slow" but cannot see that the Postgres container is
   memory-throttled.

## 2. Vision

`pgops-mcp` turns any MCP client (Claude Desktop, Cursor, VS Code, custom agents) into
a careful, expert-level Postgres operator: it inspects, queries, explains, advises,
migrates, and monitors — inside guardrails that make every action classified, confirmed
when dangerous, and permanently audited — while understanding the Docker environment
hosting the stack.

## 3. Target users

1. **Developers using AI IDEs/assistants** who want their agent to work on their local or
   self-hosted Postgres safely (primary install base)
2. **Platform/SRE teams** evaluating controlled agent access to databases
3. **Agent framework builders** who need a trustworthy DB-operations tool

## 4. Goals

| # | Goal | Success metric |
|---|---|---|
| G1 | Safe by architecture, not by prompt | Zero unguarded write paths; all destructive ops require confirmation token; audit log covers 100% of executed statements |
| G2 | Migration engine that beats "just run this DDL" | Schema diff → migration plan with lock-impact estimate → transactional apply → rollback; used successfully on a real schema |
| G3 | Performance diagnosis a human trusts | Given pg_stat_statements + EXPLAIN, produces correct bottleneck verdicts on ≥10 seeded slow-query scenarios |
| G4 | Environment awareness | Correctly maps containers→DB topology on a docker-compose stack and correlates container stats with db.health findings |
| G5 | Adoptable | Installable via uv/pipx in <2 min; works with Claude Desktop, Cursor, VS Code; published to PyPI + Smithery + official MCP registry |

## 5. Non-goals (v1)

- Not a hosted/cloud service — local & self-hosted only (that IS the product)
- No MySQL/MSSQL/SQLite support (Postgres only, deeply)
- No ORM/code generation beyond migrations
- No multi-database fleet management (single DSN per server instance)
- Not a backup/restore tool (delegated to existing tooling; may advise via tools later)

## 6. Functional requirements

### FR-1 Schema intelligence
- Inspect tables, columns, types, constraints, indexes, FKs, extensions, per-table size
- Diff two schemas (or schema vs. migration history) as a structured change set

### FR-2 Guarded querying
- Read tool: SELECT/WITH/EXPLAIN only, row-limit enforcement, statement timeout
- Write tool: INSERT/UPDATE/DELETE/DDL behind read-write role; classification step;
  unbounded mutations blocked; destructive classes require confirmation token from user
- Explain tool: EXPLAIN ANALYZE (opt-in ANALYZE) parsed into structured JSON with
  bottleneck flags (seq scan on large table, nested loop blowup, sort spill to disk)

### FR-3 Migration engine
- Plan: diff → ordered SQL steps, each annotated with estimated lock impact and
  safety notes (e.g., "ADD COLUMN with non-volatile DEFAULT: full rewrite avoided")
- Apply: versioned, checksummed, transactional where safe; records applied migrations
- Rollback: generated down-migration when feasible; refusal with explanation when not

### FR-4 Performance & health
- Health snapshot: connection counts, cache hit ratio, bloat estimates, dead tuples,
  long-running queries, locks/waiting sessions
- Index advice from pg_stat_statements: missing indexes, unused indexes, redundant indexes

### FR-5 Environment awareness (Docker)
- Topology discovery: containers, images, ports, volumes; identify Postgres container(s)
- Log tailing with severity filter for any discovered container
- Resource stats per container; correlation hints linking container pressure to DB symptoms
- Restart/exec gated behind approval mode (off by default)

### FR-6 Audit & governance
- Append-only JSONL audit log: timestamp, tool, statement, duration, rows affected, verdict
- Optional OTel spans per tool call (feature-flagged dependency)

## 7. Acceptance criteria (v0.1 public release)

1. All Phase gates in SPEC.md pass
2. Guardrail test suite proves: unbounded DELETE blocked, confirmation flow enforced,
   read-only role cannot write, audit log captures everything
3. Demo script: fresh docker-compose (app + Postgres) → inspect → explain a seeded bad
   query → advised index → planned & applied migration → rollback → health report
4. README quickstart verified on clean machine; GIFs of Claude Desktop driving the tools
5. Published: PyPI package, Smithery listing, official registry entry

## 8. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Lock-impact estimation is heuristic, can be wrong | Present as *estimates* with confidence + reasoning; never auto-apply risky DDL |
| Statement classification has gaps (functions, CTEs hiding writes) | Deny-by-default classifier: anything not recognized as safe-read goes through write path |
| Docker socket access = broad power | Read-only API usage by default; restart/exec behind explicit approval mode flag |
| Scope creep toward "database IDE" | Non-goals list enforced; every new tool needs a PRD update first |
