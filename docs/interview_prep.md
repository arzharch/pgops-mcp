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

**Q: You said "production-grade" — give me a real example, not a claim.**
A: `docker-compose.yml` originally mapped Postgres to host port 5432. On this actual
dev machine that port was already bound by an unrelated native Postgres service —
`docker compose up` would have failed or silently attached to the wrong instance.
Caught it by checking `netstat` before assuming the default would work, remapped to
5433, named the container explicitly (this machine has a dozen same-named `postgres`
containers from other projects), and added a `pg_isready` healthcheck so `docker compose
up -d` returning success actually means "seeded and queryable," not just "process
started." Separately, `uv sync` failed on first run: `uv_build` infers the importable
package name from the *normalized* project name (`pgops-mcp` → `pgops_mcp`), but the
package lives at `src/pgops`. Fixed with `[tool.uv.build-backend] module-name = "pgops"`
in `pyproject.toml`. Neither of these is exotic — they're exactly the kind of thing that
never shows up in a tutorial's "happy path" and immediately shows up on someone else's
machine.

## Section 2: Phase 1 — Connection core & read path

**Q: Why two pools (readonly/readwrite) instead of one role?**
A: Least privilege at the connection level, and — critically — enforced *by Postgres*,
not just by our code. Every connection acquired from the readonly pool runs one extra
statement on `setup`:

```python
async def _init_readonly_connection(conn: asyncpg.Connection) -> None:
    await conn.execute("SET default_transaction_read_only = on")

self._readonly_pool = await asyncpg.create_pool(
    dsn=self._config.readonly_dsn or self._config.dsn,
    min_size=self._config.pools.readonly_min,
    max_size=self._config.pools.readonly_max,
    setup=_init_readonly_connection,
)
```

`default_transaction_read_only` is a session-level GUC. Once set, Postgres refuses any
write at the *executor* level for that session — this holds even if the DSN's role is a
superuser with full GRANTs. So even if the classifier has a bug (it will, eventually —
that's why ADR-001 names known gaps), the write physically cannot execute through the
readonly pool. It's proven, not asserted: `test_connections.py` opens the readonly pool
with the test container's *superuser* DSN and shows `INSERT` is still rejected with a
"cannot execute INSERT in a read-only transaction" error.

**Alternative considered and rejected:** requiring the operator to provision a real
least-privilege Postgres role (`CREATE ROLE pgops_ro; GRANT SELECT ...`) before pgops
would even start. More textbook "least privilege," but it breaks the <2 min install goal
(G5) — most users are pointing this at a local dev DB with one role. The session-GUC
approach gives the safety guarantee unconditionally, from connection code alone, with
zero setup. `PGOPS_READONLY_DSN` still exists as an *optional* override for anyone who
does want a real second role in production — belt-and-suspenders, not required.

**Q: How does the classifier work internally?**
A: `classify()` (`classifier.py`) is allowlist-shaped, not blocklist-shaped — deny by
default per ADR-001. It does three things:

1. Split on `;` — more than one statement is rejected outright as `unknown`
   (`sqlparse.split`), because stacked queries are a classic injection shape and
   there's no legitimate reason a tool call needs two statements.
2. Tokenize the single statement with `sqlparse` and flatten it, so nesting (CTEs,
   subqueries) collapses into one linear token stream.
3. Scan **every** token — not just the leading keyword — for a `Token.Keyword.DML`
   whose value is INSERT/UPDATE/DELETE:

```python
write_hit = any(
    tok.ttype is T.Keyword.DML and tok.normalized.upper() in _WRITE_DML
    for tok in tokens
)
```

That single scan is what catches `WITH x AS (INSERT INTO orders ... RETURNING *)
SELECT * FROM x` — a statement whose *outer* shape is a harmless-looking SELECT. Because
`sqlparse` tags DML keywords with a distinct token type from string/identifier tokens,
`SELECT 'insert' AS label` does *not* false-positive (there's a test for exactly this).
Only after that check comes leading-keyword classification for DDL vs destructive
(DROP/TRUNCATE/`ALTER ... DROP COLUMN`) vs read. Anything not recognized — `DO` blocks,
`VACUUM`, `COPY`, empty input — falls to `unknown`, which `effective_gate_class` maps to
`destructive` for any consumer deciding whether to allow execution.

**Q: Why sqlparse and not a real Postgres parser (pglast/libpg_query)?**
A: `pglast` wraps libpg_query — the actual Postgres grammar — and would be strictly more
*correct*: nothing could fool its understanding of nesting or scoping. Rejected for v1
because it ships prebuilt C extensions per platform/Python version, which directly
fights G5 (`uv`/`pipx` install in under 2 minutes, works with Claude Desktop/Cursor/VS
Code across platforms). A classifier that's 100% correct but only installs cleanly on
Linux isn't safer in practice for most of this project's target users. `sqlparse` is
pure-Python, has no compiled dependency, and is *lexically* good enough for the actual
question we're asking ("is there a write DML keyword anywhere in this token stream,
outside of a string literal or comment?") — it doesn't need to understand schemas or
types to answer that. The honest gap this leaves: a `SELECT my_func()` where `my_func`
is a volatile function that writes internally is invisible to any lexer, sqlparse or
pglast — that requires a catalog round-trip (`pg_proc.provolatile`) we don't do in
Phase 1. It's named in ADR-001 as a known limitation, and it's exactly what the
read-only-pool GUC (previous answer) exists to catch regardless: the function call would
execute, but any write inside it would be refused by Postgres itself.

**Q: Why not just wrap the query in `SELECT * FROM (...) sub LIMIT n` to enforce the row
cap?**
A: Rejected — it's fragile in ways that matter for a tool taking arbitrary agent-written
SQL: breaks on `EXPLAIN ...` (can't wrap an EXPLAIN in a subquery), breaks on a trailing
semicolon, breaks on CTEs whose outer SELECT references CTE aliases in ways a wrapper
subquery changes the scope of, and it's one more place untrusted SQL text gets
string-manipulated right before execution — exactly the kind of surgery that tends to
grow an injection bug over time. Instead, `query.read` opens a server-side cursor on the
*unmodified* SQL and only ever pulls `limit + 1` rows from it:

```python
async with conn_manager.readonly_pool.acquire() as conn, conn.transaction():
    await conn.execute(f"SET LOCAL statement_timeout = {resolved_timeout}")
    cursor = await conn.cursor(sql)
    records = await cursor.fetch(resolved_limit + 1)
```

Postgres itself stops producing rows past that point. Fetching `limit + 1` instead of
`limit` is what lets the response report `truncated: true/false` without a second round
trip (`COUNT(*)` over arbitrary agent SQL would be its own can of worms).

**Q: Why `SET LOCAL statement_timeout` instead of setting it once per pooled
connection?**
A: `LOCAL` scopes the setting to the current transaction — it reverts automatically at
COMMIT/ROLLBACK. Pooled connections are reused across unrelated calls; if a timeout were
set at the session level instead, one caller's tight timeout (or one caller's absence of
a timeout) could leak onto the *next* caller that happens to reuse the same physical
connection from the pool. Wrapping the whole read (`SET LOCAL` + cursor open + fetch) in
one explicit `conn.transaction()` block is what makes that safe.

## Section 2b: Tuning for different environments

Every number below is a `PGOPS_*` env var (`config.py`) with a conservative default —
these are the actual tuning knobs an interviewer question like "how would you run this
in production vs. on a laptop" maps onto:

| Setting | Dev/laptop default | Why you'd raise it in production |
|---|---|---|
| `PGOPS_READONLY_POOL_MAX` (5) | fine for one agent session | multiple concurrent MCP clients (several engineers' Claude Desktops against a shared dev DB) need pool headroom — but weigh against Postgres `max_connections` (default 100) shared with the app itself |
| `PGOPS_READWRITE_POOL_MAX` (2) | writes are rare/manual | kept intentionally small even in prod — the audit log and the "one server, one write queue" model (ARCHITECTURE.md failure modes) mean write concurrency isn't the goal; correctness and auditability are |
| `PGOPS_DEFAULT_TIMEOUT_MS` (5000) / `PGOPS_MAX_TIMEOUT_MS` (30000) | generous for exploratory queries against a 1.2M-row dev table | production analytical queries against a much larger table may legitimately need a higher `max`, but raising the *ceiling* is a deliberate operator decision (env var), never something a single tool call can override past |
| `PGOPS_DEFAULT_ROW_LIMIT` (100) / `PGOPS_MAX_ROW_LIMIT` (10000) | keeps an agent from accidentally dumping a huge table into its own context window | production data-export use cases would raise `MAX_ROW_LIMIT`, not remove the cap — the cap is what stops "SELECT * FROM orders" from ever being unbounded, regardless of what the agent asked for |
| `PGOPS_READONLY_DSN` (unset → falls back to `PGOPS_DSN`) | one shared role is fine locally | production should point this at a real least-privilege Postgres role as a second, independent layer on top of the `default_transaction_read_only` session guarantee |
| `PGOPS_READ_ONLY` (off) | writes available for local iteration | flip on for a demo environment, a read replica, or handing the server to someone you don't fully trust yet — hard-disables the write pool at the `ConnectionManager` level (`readwrite_pool()` raises before ever calling `asyncpg.create_pool`) |

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
