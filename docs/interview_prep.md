# Interview Prep — Growing Q&A Companion

> As features land, the interview questions they invite get answered HERE, in writing.
> Rule: if you can't answer a question below confidently, the feature isn't done.

---

## Section 1: Project framing (answerable NOW)

**Q: What is pgops-mcp in one sentence?**
A: An MCP server that gives AI agents safe, audited, expert-level operations over a real
PostgreSQL database and its Docker environment — guarded queries, migration planning with
lock-impact analysis, performance diagnosis from EXPLAIN and workload stats, and container
awareness — all through tools instead of shell access.

**Q: Don't Postgres MCP servers already exist?**
A: Yes, but they're introspection + query wrappers. None analyze lock impact before DDL,
none turn EXPLAIN output into actionable verdicts, none correlate database health with
container metrics. I verified this across the official registry, Smithery, and mcp.so.
The depth is the product; the safety architecture is the moat.

**Q: Why is this hard? It's just API calls to Postgres.**
A: Three genuinely hard parts: (1) the safety architecture — classifying arbitrary SQL
deny-by-default and making destructive actions impossible without explicit confirmation;
(2) the migration engine — schema diffing, dependency ordering, transactional DDL
semantics, honest lock-duration estimation, down-migration generation; (3) performance
diagnosis — parsing EXPLAIN plans into verdicts that are actually correct against real
Postgres behavior, proven by seeded scenarios in tests.

**Q: How do you stop an agent from running `DELETE FROM orders` without a WHERE?**
A: Deny-by-default classifier (ADR-001): every statement is classified before execution;
unbounded mutations are blocked and return a single-use confirmation token plus a
human-readable reason. The agent relays the reason to the user; only a re-invocation with
the unexpired token executes. Everything lands in an append-only audit log either way.
And it's proven by tests against real Postgres, not mocks (ADR-005).

**Q: What if your classifier mislabels something?**
A: Two failure directions: safe-labeled-but-dangerous is the catastrophic one — mitigated
by deny-by-default (unknown = dangerous) and table-driven tests covering CTE-wrapped
writes and volatile functions. Dangerous-labeled-but-safe just costs a confirmation click,
which is the right trade.

**Q: Why stdio and not HTTP?**
A: Target users run Claude Desktop/Cursor locally against local Postgres — stdio is
native, zero network attack surface, no auth problem to solve badly. The tool layer is
transport-agnostic so HTTP can be added later for remote use (ADR-002).

## Section 2: Phase 1 — Connection core & read path (populate as you build)

**Q: Why two pools (readonly/readwrite) instead of one role?**
A: (to fill — least privilege at connection level; readonly role enforced BY POSTGRES,
not just by our code, so even a classifier bug can't write through the read path)

**Q: How does the classifier work internally?**
A: (to fill after building — expect: statement type detection, CTE inspection,
function volatility checks, deny-by-default fallthrough)

## Section 3: Phase 2 — Safety architecture (populate as you build)

**Q: Walk me through the confirmation token lifecycle.**
A: (to fill — issuance on refusal, TTL, single-use, binding to statement hash)

**Q: What's in the audit log and how would you use it in an incident?**
A: (to fill)

## Section 4: Phase 3 — Performance brain (populate as you build)

**Q: How do you know your EXPLAIN verdicts are correct?**
A: (to fill — seeded scenario suite: each fixture has a known defect and expected verdict)

**Q: What does estimate-vs-actual row divergence tell you?**
A: (to fill — stale statistics → ANALYZE hint; misestimates → bad plan shapes)

## Section 5: Phase 4 — Migration engine (populate as you build)

**Q: Which ALTERs are metadata-only vs rewrite in Postgres, and why does it matter?**
A: (to fill — ADD nullable column cheap; TYPE changes rewrite; NOT NULL needs scan unless
default + existing validation path; index creation locking vs CONCURRENTLY trade-offs)

**Q: How does crash recovery work mid-migration?**
A: (to fill — ledger statuses, transactional steps, verify-on-resume)

## Section 6: Phase 5 — Docker layer (populate as you build)

**Q: Isn't giving agents Docker access dangerous?**
A: (to fill — read-only API default; restart/exec double-gated: server flag AND token)
