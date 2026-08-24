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
| `PGOPS_READ_ONLY` (off) | writes available for local iteration | flip on for a demo environment, a read replica, or handing the server to someone you don't fully trust yet — removes `query.write` from the advertised tool list *and* hard-disables the write pool |
| `PGOPS_POOL_ACQUIRE_TIMEOUT_MS` (10000) | one agent, contention unlikely | with several clients sharing a pool this is what converts "the agent stopped responding" into a clear `POOL_EXHAUSTED` error; lower it if you'd rather fail fast than queue |
| `PGOPS_CONFIRM_TOKEN_TTL_S` (300) | 5 min is comfortable for a human to read a reason and decide | shorten it where approvals must be deliberate and immediate; lengthening it widens the window in which an approved-but-unexecuted destructive statement can still fire |
| `PGOPS_AUDIT_LOG` (`~/.pgops/audit.jsonl`) | per-user local file | point at a directory shipped to your log pipeline; note the write is `fsync`'d per entry, so a network filesystem will slow every write call |

## Section 3: Phase 2 — Safety architecture

**Q: Walk me through the confirmation token lifecycle.**
A: Five stages, and the interesting design decision is at stage 3.

1. **Refusal.** `query.write("DELETE FROM orders")` → classifier says WRITE → guardrails
   see no WHERE clause → refused. A token is minted and returned inside the error hint,
   along with a human-readable reason. Nothing executed.
2. **Relay.** The agent shows the user the reason. It cannot mint a token itself, so
   human approval is structurally required, not politely requested.
3. **Binding.** The token is bound to `sha256(sql)`:

```python
def issue(self, sql: str, reason: str) -> str:
    token = secrets.token_urlsafe(24)
    self._tokens[token] = _Issued(
        sql_hash=sql_fingerprint(sql),
        expires_at=time.monotonic() + self._ttl_s,
        reason=reason,
    )
    return token
```

   This is the part that matters. A token meaning only "the user approved *something*"
   is forgeable-by-confusion: an agent could obtain approval for `DELETE FROM staging`
   and redeem it against `DELETE FROM orders`. Binding to the statement hash makes the
   approval specific to what was actually shown to the user.
4. **Redemption.** `redeem(token, sql)` raises on every failure mode and returns `None`
   on success — deliberately not a boolean, because a caller who forgets to check a
   boolean fails *open*, while one who ignores an exception cannot. A hash mismatch
   raises `CONFIRMATION_MISMATCH` and — importantly — does **not** consume the token:
   the mismatch means this call wasn't the approved one, so the user's real pending
   approval is still legitimate and must survive.
5. **Expiry.** Single-use (deleted on successful redeem), 5-minute TTL, in-memory only.
   In-memory is a deliberate choice: a persisted token would mean approvals survive a
   restart the user never saw. Losing a token costs one extra confirmation; honoring a
   stale one costs a table.

`secrets.token_urlsafe`, not `random` or `uuid4()`: this value is an authorization
credential, and a CSPRNG costs nothing here.

**Q: How do you detect an "unbounded" DELETE? Isn't that just checking for WHERE?**
A: Checking for WHERE, yes — but *how* you check is the whole thing. The naive version
is `"where" in sql.lower()`, and it's wrong in three ways that all fail open:

```python
("INSERT INTO log (msg) VALUES ('where')", False),   # literal containing the word
("UPDATE orders SET wherefore = 1",         False),   # identifier containing it
("DELETE FROM orders -- WHERE id = 1",      False),   # commented-out clause
```

Each of those makes a substring check believe a bounded statement is present. The last
one is the dangerous one: a real unbounded `DELETE FROM orders` that a substring check
waves straight through because the word appears in a trailing comment. So detection runs
over the sqlparse token stream and looks for an actual `Token.Keyword` WHERE — literals,
identifiers, and comments all carry different token types and can't be confused for it.
Those exact cases are parametrized tests in `test_guardrails.py`.

**Q: What's in the audit log and how would you use it in an incident?**
A: Append-only JSONL — timestamp, tool, verdict, classification, SQL, SHA-256 of the
SQL, duration, rows affected, error code. The design decision worth defending is *what
gets logged*: every executed statement **and every refusal**. A "log what we ran" design
would show nothing for a blocked `DELETE FROM orders`, which is exactly the event an
incident review is looking for. Here's a real trail from the Phase 2 gate run:

```json
{"verdict":"refused_pending_confirmation","sql":"DELETE FROM orders","detail":"DELETE has no WHERE clause and would affect every row in the table"}
{"verdict":"refused_bad_token","sql":"DROP TABLE orders","error_code":"CONFIRMATION_MISMATCH"}
{"verdict":"executed","sql":"UPDATE orders SET status = 'paid' WHERE id <= 3","rows_affected":3,"duration_ms":110.0}
```

An incident reviewer can reconstruct the whole sequence: something dangerous was
attempted, it was blocked, someone tried to reuse that approval for a *different*
destructive statement, that was blocked too, and here's what actually ran and how many
rows it touched. The SHA-256 lets you group identical statements across records without
string-matching over SQL that may embed literal values.

**Q: Why JSONL in a file, and not a table in the database you're already connected to?**
A: Circularity. The audit trail for "who dropped that table" has to survive the database
being broken — if the log lives in the same Postgres the agent is operating on, the
statement you most need recorded is the one that can destroy its own record. It also
must not be writable by the statements it audits. A local file is independent of the
target's health.

JSONL specifically, over a single JSON array: append-only by construction. No read-modify-
write step, so an interrupted process can't corrupt the file — worst case the final line
is torn, and `read_all()` discards that one line and returns everything before it intact
(there's a test that truncates a line mid-write and asserts the rest still parses). It's
also greppable with standard tools, which matters at 3am.

**Q: What if the audit write fails?**
A: It logs loudly at ERROR level and the tool call still succeeds. That's a deliberate
trade and worth stating explicitly: the alternative — failing the operation because we
couldn't record it — sounds more rigorous but means a full disk turns into a total
outage of a tool the user is mid-task with. For a local developer tool that's the wrong
trade. In a compliance setting where the audit trail is a hard requirement, you'd invert
it and fail closed; it's one line in `audit.py`.

**Q: Why does `--read-only` remove the write tool instead of refusing calls to it?**
A: Both are safe, but removing it from `list_tools()` is better: an agent can't be
tempted by, or waste turns on, a tool it was never told exists. Refusing at call time
means the model sees a capability advertised, plans around it, tries it, and gets an
error it then has to recover from. The `ConnectionManager` *also* refuses at the pool
level (`readwrite_pool()` raises before ever calling `create_pool`), so it's belt and
suspenders — but the advertised surface is the first line.

## Section 3b: The Phase 1 review — bugs I found in my own code

Good material for "tell me about a bug you found" or "how do you know your code works".
All four were found by auditing Phase 1 *before* starting Phase 2, and all four now have
regression tests.

**Q: Tell me about a bug you caught before it shipped.**
A: `schema.inspect(level="full")` was broken for every caller and my tests were green.
`pg_constraint.contype` is Postgres's internal `"char"` type, which asyncpg decodes to
Python `bytes` — a perfectly valid Python object that `json.dumps` cannot encode. Since
MCP results are JSON, the tool failed at the serialization boundary, not in my code. It
survived because I'd verified with `--selfcheck` and a manual call, and *both only
exercised `level="summary"`*, which doesn't touch constraints.

The lesson I actually took from it: I was testing the tool functions directly, which
skipped the layer where the failure lived. The fix was structural — route all catalog
output through one `serialize_value` helper, and add tests that call through the real
FastMCP server object so the JSON encoding boundary is exercised:

```python
for name, args in calls:
    result = await server.call_tool(name, args)
    assert result.is_error is False
    json.dumps(result.structured_content)
```

`db.health` had the identical latent bug (`dead_pct` is a Postgres numeric → `Decimal`),
and its JSON test had been passing only because a freshly seeded container has no dead
tuples — the branch never ran. That test now generates UPDATE churn first, so the
assertion actually reaches the code path it claims to cover.

**Q: How do you make sure an internal error never reaches the user?**
A: Originally each tool had `except PgopsError: return exc.to_dict()`, which by
construction only catches failures I already anticipated — everything unforeseen
propagated out with a traceback. I proved it: `schema.inspect(table="Order Items")`
raised a raw `InvalidNameError` straight through the tool layer. Two fixes:

1. The specific cause — I was passing the table name into `$1::regclass`, which parses
   its argument as an identifier *expression*, so any name needing quoting blows up.
   Rather than reimplement Postgres's identifier-quoting rules in Python, I keyed every
   catalog query off `pg_class.oid` and removed name parsing entirely.
2. The general class — one `tool_boundary` decorator wrapping every tool, with the catch
   order inverted: known errors first, then a catch-all that logs the traceback to
   stderr and returns a generic code.

```python
except PgopsError as exc:
    return exc.to_dict()          # expected, actionable, safe to show
except Exception:
    logger.exception(...)          # operator sees everything, on stderr
    return PgopsError(ErrorCode.INTERNAL_ERROR, "internal error; see server logs").to_dict()
```

There's a test asserting a secret in an exception message (`postgres://user:hunter2@...`)
does not appear anywhere in the returned payload.

**Q: Any performance work?**
A: `schema.inspect` was an N+1: three catalog queries per table, so `level="full"` on a
200-table schema meant 600 round trips, where network latency — not Postgres — dominates
the response. Collapsed to three total by passing the whole OID array:
`WHERE a.attrelid = ANY($1::oid[])`, then fanning results back out by `table_oid` in
Python. Same data, three round trips regardless of schema size.

**Q: Anything you'd flag about how you detect lock contention?**
A: I originally wrote the `pg_locks` self-join that circulates on blogs — join
`granted`/`not granted` rows on locktype/database/relation. It's subtly wrong: those
columns don't identify every lock type (tuple, transactionid, virtualxid, advisory all
fall through), and it reports false positives for lock modes that don't actually
conflict. Replaced with `pg_blocking_pids()`, which is Postgres's own answer and consults
the real lock manager including conflict-mode rules and parallel-worker leaders. Being
wrong about "who is blocking production right now" is worse than not reporting it.

**Q: What about the stdio transport — any gotchas?**
A: One that's easy to get wrong and silent when you do: under stdio, **stdout is the MCP
protocol channel**. The client parses it as a stream of JSON-RPC messages, so a single
stray log line or `print()` corrupts the stream, and the failure surfaces as what looks
like a client bug. `logging.StreamHandler` defaults to stderr anyway, but I set it
explicitly with a comment, because the consequence of a future edit getting it wrong is
so disproportionate to how obvious the mistake looks.

**Q: What's the weakest part of the system right now?**
A: The classifier can't see writes inside a volatile function — `SELECT my_func()` where
`my_func` does an INSERT internally is invisible to any lexer, sqlparse or pglast alike.
Closing it properly needs a `pg_proc.provolatile` catalog lookup per function reference,
which is Phase 3+ work. Right now it's caught one layer down: the read-only pool means
Postgres itself refuses the write at execution time. I verified that empirically rather
than assuming it — including two cases I expected to slip through and didn't:

```
SELECT id FROM items LIMIT 1 FOR UPDATE  -> cannot execute SELECT FOR UPDATE in a read-only transaction
SELECT nextval('items_id_seq')           -> cannot execute nextval() in a read-only transaction
```

Both are lexically SELECTs with real side effects — row locks that block the application,
and sequence state that can't be rolled back. That's the clearest evidence I have that
the layered design is doing real work rather than just sounding good: the layer that
catches these is not the one designed to.

## Section 4: Phase 3 — Performance brain

**Q: How do you know your EXPLAIN verdicts are correct?**
A: Two layers. Unit tests over synthetic plan JSON assert the *arithmetic* exactly —
loop multiplication, self-time subtraction, parallel handling — because those need
plans with known numbers. Then 18 seeded scenarios in `test_explain.py` run against
real Postgres and assert the rules fire on plans **Postgres actually chose**, which is
the only way to know a threshold matches reality rather than my assumptions.

The negative scenarios matter as much as the positive ones: an indexed lookup, a
primary-key lookup, and a small-table scan must produce *no* verdict. A rule set that
flags something on every query is noise, and a reader who learns to ignore the verdict
list gets nothing from the one time it's important.

**Q: What's the hardest thing to get right when parsing an EXPLAIN plan?**
A: Loops, and I got it wrong the first time in a way the database caught for me.

`Actual Rows`, `Actual Total Time`, and `Plan Rows` are all **per loop**, not totals. A
node reporting `Actual Rows: 80000, Actual Loops: 3` returned 240,000 rows. So you
multiply by loops — and that's where the subtlety is, because *loops don't always mean
the same thing*. Under a Nested Loop, `Actual Loops` counts sequential iterations, so
time genuinely multiplies. Under a Gather, it counts **concurrent parallel workers** —
rows still sum, but wall-clock time does not, because they ran at the same time.

Multiplying time by loops everywhere produced this against the dev database:

```
[info] dominant_node: 5180ms of 2400ms total (216%) is spent in this node alone
```

216% is not just wrong, it's *obviously* wrong, which is why I'd rather find it that way
than in a subtly plausible number. The fix propagates a `parallel` flag while descending
through Gather/Gather Merge nodes:

```python
child_parallel = parallel or node_type in _GATHER_NODES

@property
def total_time_ms(self) -> float | None:
    if self.parallel:
        return self.actual_total_time_ms      # workers overlap; don't sum
    return self.actual_total_time_ms * self.actual_loops
```

Same query now reports 876ms of 1248ms (70%). There's a regression test asserting no
node can own more than 100% of execution time.

**Q: Why report "self time" instead of total time?**
A: A node's `Actual Total Time` includes all of its children, so ranking nodes by total
time always names the root — which tells you nothing, since of course the whole query
took the whole query's time. Self time (total minus children's totals) is what actually
localizes the cost. That's the difference between "your query is slow" and "6 of your 8
seconds are in this one sequential scan".

**Q: What does estimate-vs-actual row divergence tell you?**
A: It's usually the *root cause* rather than a symptom. Postgres picks join strategies
from estimated cardinalities — expecting 10 rows it chooses a nested loop, expecting 10
million it chooses a hash join. When the estimate is off by 10x+, the plan shape is
chosen for a query that doesn't exist, and everything downstream looks inexplicable.

Two causes worth distinguishing: stale statistics (fix: `ANALYZE`) and **correlated
columns**. The planner assumes independence, so for `WHERE a = 1 AND b = 1` it multiplies
the two selectivities. If `a` and `b` are perfectly correlated it underestimates by the
cardinality of one of them. That's what the seeded divergence scenario builds, and the
suggestion points at `CREATE STATISTICS`, which is the actual fix — `ANALYZE` alone will
never help there.

I only flag divergence above 1,000 rows: 1 estimated vs 50 actual is a 50x ratio that
changes no plan decision.

**Q: `EXPLAIN` is read-only, so `query.explain` is a safe tool, right?**
A: No — and this is the trap I think is worth catching in an interview. `EXPLAIN ANALYZE
DELETE FROM orders` **performs the delete**. `ANALYZE` means "execute it and report real
timings". A tool that treats "explain" as inherently read-only will eventually delete a
production table because somebody wanted to know why a query was slow.

So `analyze=false` (the default) never executes. `analyze=true` on a mutating statement
runs inside a transaction that is always rolled back, *and* goes through the same
guardrail, confirmation-token, and audit path as `query.write`. I deliberately did not
waive the confirmation on the grounds that it's rolled back, because rollback is not a
complete undo: sequence values consumed by `nextval()` don't roll back, and neither do
side effects inside functions the statement calls.

The rollback is structural rather than conditional:

```python
async with pool.acquire() as conn, conn.transaction():
    raw = await conn.fetchval(explain_sql)
    captured = json.loads(raw) if isinstance(raw, str) else raw
    raise _Rollback          # exits the block as a failure -> transaction aborts
```

A `try/finally` calling rollback would be weaker, and an early `return` inside the block
would skip it entirely. There's no path where that transaction commits. Tests assert the
rows survive and that `execution_time_ms > 0`, i.e. it really did run.

**Q: Your advisor recommends dropping indexes. How do you know they're really unused?**
A: I don't, on a short window — and finding that out is the most useful thing that
happened in this phase. Running the advisor against the live database, it told me:

```
unused idx_orders_customer_id (10764288 bytes): DROP INDEX idx_orders_customer_id;
```

That index had been used *seconds earlier* by the explain query I'd just run. `pg_stat`
counters lag, so `idx_scan` read a stale `0`. Had I followed my own tool's advice I'd
have dropped a working index off a 1.2M-row table.

`idx_scan = 0` genuinely has two causes — "nothing uses this" and "we haven't been
watching long enough" — and they're indistinguishable from the counter alone. So the
tool now measures the observation window from `pg_stat_database.stats_reset`, reports it
in the response, and gates the recommendation on it. Under 7 days:

```json
{"confidence": "low",
 "suggestion": "do NOT drop idx_recently_used yet — statistics have only been collected
                for 4 minutes, which is too short to conclude it is unused..."}
```

Seven days because a weekly reporting job needs a week to show up. The general principle
is the one from ADR-004: confidently wrong advice is worse than no advice, because it
discredits every other finding the tool produces.

**Q: What else does the advisor deliberately refuse to say?**
A: Two things. First, it never reports primary-key or unique indexes as unused or
redundant. `UNIQUE (email)` is not superseded by `(email, region)` — the composite
serves the same *queries* but does not enforce the same *rule*. Conflating those is how
an advisor talks someone into dropping their uniqueness guarantee.

Second, for missing indexes it names the table taking sequential scans but not the
column to index, because it can't know that without inspecting a plan. The output is
"here's the evidence, run `query.explain` on this statement" rather than a fabricated
`CREATE INDEX` that looks authoritative. That's a deliberate honesty/usefulness trade,
and it's on the roadmap to close properly by feeding statements from
`pg_stat_statements` through the plan analyzer.

**Q: What if `pg_stat_statements` isn't installed?**
A: It's an extension requiring `shared_preload_libraries` and a restart, so plenty of
databases won't have it. Its absence degrades the tool to catalog-only findings plus a
note explaining how to enable it — it doesn't fail the call. There's also a `try/except`
around reading it when it *is* present, because the column names changed across major
versions (`total_time` became `total_exec_time` in PG13); a version mismatch degrades
the same way rather than taking out the whole response.

## Section 5: Phase 4 — Migration engine (populate as you build)

**Q: Which ALTERs are metadata-only vs rewrite in Postgres, and why does it matter?**
A: (to fill — ADD nullable column cheap; TYPE changes rewrite; NOT NULL needs scan unless
default + existing validation path; index creation locking vs CONCURRENTLY trade-offs)

**Q: How does crash recovery work mid-migration?**
A: (to fill — ledger statuses, transactional steps, verify-on-resume)

## Section 6: Phase 5 — Docker layer (populate as you build)

**Q: Isn't giving agents Docker access dangerous?**
A: (to fill — read-only API default; restart/exec double-gated: server flag AND token)
