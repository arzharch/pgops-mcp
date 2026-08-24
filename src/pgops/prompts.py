"""MCP prompts — user-invoked workflow templates.

Prompts are the third server primitive and the one most often skipped. They are
*user*-controlled (a slash command, a menu entry), not model-controlled, and that makes
them the right home for something tools cannot express: **the order in which to use the
tools, and what to do with the answers.**

The tools here each do one thing well, but knowing that a slow-query investigation should
go `query.explain` → read the verdicts → `index.advise` → propose
`CREATE INDEX CONCURRENTLY` → check `env.correlate` before blaming the query, is
operational judgment. Without prompts that judgment lives only in whatever the user
happens to type, and gets re-derived — differently, and sometimes wrongly — every
session.

Each prompt below is written to constrain the model toward the safe path: verify before
acting, prefer the concurrent/online variant, never skip the confirmation step, and say
"I don't know" rather than guess at a cause.
"""

from __future__ import annotations


def diagnose_slow_query(sql: str) -> str:
    return f"""Diagnose why this query is slow, and do not guess.

```sql
{sql}
```

Work in this order:

1. Call `query.explain` with `analyze=true` (unless the statement mutates data — then
   start with `analyze=false` and say why).
2. Read the `verdicts` array. Each one carries `evidence`; quote the specific numbers
   rather than paraphrasing. The `dominant_node` verdict tells you where the time
   actually goes — start there, not at the top of the plan.
3. If a verdict reports `estimate_divergence`, treat that as a likely *root cause*
   rather than a symptom: the planner chose this plan shape from a wrong row estimate.
   Recommend ANALYZE, or CREATE STATISTICS when the columns look correlated.
4. Call `index.advise` and check whether the table already has an unused or redundant
   index before proposing a new one.
5. Before concluding the query is at fault, call `env.correlate` — a CPU-throttled or
   memory-starved container makes every query slow regardless of how it is written.

When you propose an index, use `CREATE INDEX CONCURRENTLY` and say what it will cost:
`migration.plan` will give you a lock-impact estimate. State clearly if you cannot
determine the cause from the evidence available."""


def plan_safe_migration(description: str) -> str:
    return f"""Plan this schema change so it can be applied without downtime.

Requested change: {description}

1. Read the current schema first (`schema.inspect`, or the `pgops://schema` resource).
2. Express the desired end state as a `migration.plan` target. Do not write raw DDL —
   the planner orders steps and analyses locks for you.
3. Leave `allow_drops` false unless the user explicitly asked to remove something.
   A table absent from a target is usually an incomplete description, not a deletion
   request.
4. Read `lock_impact` on every step and report it in plain language. Pay attention to:
   - `rewrites_table: true` — the table is copied; reads and writes both block
   - `blocks_reads: true` with an estimate over a second — this is user-visible downtime
   - `transactional: false` — the migration is NOT atomic
5. Where a step offers a `safe_alternative`, present it as the recommended path and
   explain the trade-off (usually: more steps, more elapsed time, no downtime).
6. Confirm `dry_run_ok` is true before suggesting the user apply anything.

Report the plan and its risks. Do not call `migration.apply` until the user has seen the
lock impact and agreed."""


def incident_triage() -> str:
    return """The database is behaving badly. Triage it, cheapest checks first.

1. `db.health` — look at waiting locks and long-running queries before anything else.
   A blocked session chain is the most common cause of "everything is slow" and the
   fastest to confirm.
2. If sessions are blocked, identify the blocking PID from the `waiting_locks` finding
   and report what it is running. Do not kill anything without explicit approval.
3. `env.correlate` — check whether the container is memory-starved or CPU-throttled.
   Throttling in particular is invisible from inside Postgres.
4. `container.logs` on the database container with `min_severity=ERROR` for the last few
   minutes: look for FATAL, PANIC, checkpoint warnings, or out-of-memory kills.
5. `migration.history` — check for an interrupted (`in_flight`) migration. A crashed
   migration can leave the schema half-changed and locks held.
6. Only then look at query plans.

Report findings in severity order with the evidence for each. If the cause is not clear
from the evidence, say so and state what you would need to narrow it down further."""


def review_index_health() -> str:
    return """Review this database's indexes and recommend changes.

1. Call `index.advise`.
2. Check `stats_window` FIRST. If `sufficient_for_unused_index_advice` is false, the
   statistics have not been collecting long enough to conclude any index is unused —
   report the findings as provisional and say what the window is. Do not recommend
   dropping an index on a short window, however tempting the size saving looks: a weekly
   reporting job needs a week of statistics to show up.
3. Redundant (prefix) indexes are safe to recommend dropping — they come from catalog
   structure, not usage statistics. Note that unique and primary-key indexes are
   excluded because they enforce constraints, not just serve queries.
4. For `scan_hotspots`, run `query.explain` on a representative query against that table
   to identify which column actually needs the index. Do not invent a CREATE INDEX from
   the table name alone.
5. Express any changes as a `migration.plan` so the user sees the lock impact before
   applying."""


def explain_safety_model() -> str:
    return """Explain to me what this server will and will not let an agent do.

Read the `pgops://config` resource for the live settings, then explain:

- Which tools are currently registered, and which are hidden by the current mode
  (`--read-only` hides write tools; container mutation tools are hidden unless
  `--approval-mode` is set).
- What happens when a destructive statement is attempted: classification, guardrail
  evaluation, the confirmation token, and the audit record written on refusal as well
  as on execution.
- What the read path physically cannot do, and why (the readonly pool sets
  `default_transaction_read_only`, which Postgres enforces at the executor level
  regardless of the role's privileges).
- Where the audit log is written.

Be precise about the difference between "refused by policy" and "impossible" — they are
different guarantees and the second is much stronger."""
