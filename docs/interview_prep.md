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

## Section 0: MCP protocol depth (asked as "is this actually a complete MCP server?")

**Q: MCP has tools, resources and prompts. Do you use all three?**
A: Now, yes — and I didn't at first, which is worth admitting. Phases 1–5 shipped tools
only, because tools are what the interesting work needed. The gap is real though:

- A **tool** is model-controlled. The agent decides to call it, and each call costs a
  turn and a round trip.
- A **resource** is application-controlled. The client can attach it as context up front
  without the model spending a turn deciding to fetch it.
- A **prompt** is user-controlled — a slash command or menu entry.

The clearest example: "what does my schema look like" is background context for nearly
every database conversation. As a tool the model pays a turn for it every time. As
`pgops://schema` a client attaches it once for the session. Same data, completely
different economics.

**Q: What do prompts give you that tools don't?**
A: Ordering and judgment. Each tool does one thing, but knowing that a slow-query
investigation goes `query.explain` → read the verdicts → `index.advise` → check
`env.correlate` *before* blaming the query — that's operational knowledge no individual
tool can express. Without prompts it lives only in whatever the user happens to type, and
gets re-derived differently every session.

They're also where I put the safety guidance that isn't enforceable in code: "check
`stats_window` before recommending a DROP", "never call `migration.apply` before the user
has seen the lock impact". There are tests asserting the prompts still contain that
guidance, because a prompt that quietly loses it is just documentation.

**Q: You mentioned elicitation. What problem does it actually solve?**
A: The most interesting security question in the project, and it exposed a real weakness
in my own design.

The confirmation-token protocol routes human approval **through the agent**. The server
refuses, hands back a token plus a reason, and trusts the model to relay that reason
faithfully to a human and only come back once a human said yes. *Nothing enforced the
middle step.* A model that's confused, over-eager, or adversarially prompted can just
call again with the token it was handed a moment ago.

Elicitation is a server→client request that asks the **user** directly, outside the
model's turn. The model can't fabricate the answer because the model isn't in that path.

The policy is the part I'd want to be asked about:

```
elicitation supported   -> ask the human directly
elicitation unavailable -> fall back to the token protocol
                        -> NEVER fall back to "allowed"
```

Losing elicitation degrades approval from "the human was asked" to "the agent asserts the
human was asked" — weaker, but still gated. And a human "no" raises
`CONFIRMATION_DECLINED` and issues **no token**: an explicit refusal must not be
convertible into a credential the agent redeems seconds later. The audit log records
which method approved each action, because those are different assurances.

**Q: Any bug come out of that work?**
A: A deprecation warning that turned out to be a real client bug. Calling `ctx.elicit()`
without a `response_type` produces an empty schema, and the warning said it "causes some
clients (e.g. VS Code) to render an empty, non-functional form". VS Code is a target
client in my PRD — so users would have been asked to approve a destructive action with no
way to answer. Fixed by sending an explicit `["approve", "cancel"]` choice.

I also handled a subtlety I nearly missed: the client can *accept the prompt* while the
user picks "cancel". Treating the envelope as the answer would have approved every action
the user actively declined.

**Q: What about sampling?**
A: Not used yet, and I'd rather say that than claim it. Worth being precise about what it
does, because it's commonly described backwards: sampling lets the **server** request an
LLM completion **from the client's** model. So the server never needs its own API key —
it borrows the caller's.

Real candidates here: summarising an EXPLAIN plan in prose, or turning "add a nullable
note column to orders" into a `migration.plan` target. Both are on the roadmap in
flow.md rather than half-built.

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

## Section 1b: Auth and remote access

**Q: Your server has no authentication. Isn't that a problem?**
A: Over stdio, no — and I'd push back on the premise. The server is a subprocess the
user's own MCP client spawns. It inherits their privileges, listens on no port, and has
no remote caller to authenticate. The DSN comes from the client's config. Adding a token
there would be theatre: whoever can start the process already has everything the token
would protect.

That reasoning collapses the instant it listens on a port, so auth is bound to the
**transport** rather than being a global flag. `--transport http` refuses to start
without `--public-key`:

```
pgops-mcp: --transport http requires --public-key.
  generate one with:  pgops-mcp keygen
  refusing to expose database tools on a network port without auth.
```

Failing to start is deliberate. A default-insecure HTTP mode is the kind of thing that
ends up on a shared network "just for testing".

**Q: Walk me through issuing a credential for an agent.**
A: Three commands, all in the same binary:

```bash
pgops-mcp keygen                                     # RS256 keypair
pgops-mcp issue-token --subject deploy-bot --scope pgops:read --scope pgops:write
pgops-mcp --transport http --public-key ~/.pgops/keys/pgops_public.pem
```

Two design decisions worth defending:

**Asymmetric, not a shared secret.** The server holds only the public key. A server
compromise leaks the ability to *verify* tokens, never to *issue* them — the attacker
can't mint themselves an admin credential from what's on the box.

**Read-only by default.** `issue-token` with no `--scope` produces a token that cannot
write. An agent whose job is answering questions about a schema should not hold a
credential capable of dropping it, and the safe default should be the lazy one.

**Q: How do the scopes map?**
A: To the same danger tiers the guardrails already use, so the split is meaningful rather
than decorative:

| Scope | Tools |
|---|---|
| `pgops:read` | schema.inspect, query.read, query.explain, db.health, index.advise, **migration.plan**, migration.history, env.*, container.logs/stats |
| `pgops:write` | query.write, **migration.apply**, **migration.rollback** |
| `pgops:admin` | container.restart, container.exec |

`migration.plan` sits on the read side because it executes nothing — its dry run happens
inside a transaction that's always rolled back. `migration.apply` and
`migration.rollback` don't — a rollback can destroy data just as an apply can — so they
don't.

And a tool with no scope entry requires `admin` — deny-by-default, the same principle as
the SQL classifier (ADR-001). A tool added later without a scope mapping is locked down
rather than silently public, which is the failure direction that actually matters.

There's a test asserting no mutating tool appears under `pgops:read`, because the scope
split is only worth anything if a read token genuinely cannot mutate.

**Q: What's still missing for a real multi-tenant deployment?**
A: Two things, and I'd rather name them than let someone find them.

**Per-session DSN isolation.** Auth identifies the caller, but every authenticated caller
currently shares one `ConnectionManager` and one audit log. That's correct for stdio —
one user, one database — and insufficient for genuine multi-tenancy. Scoped tokens limit
*what* a caller can do, not *which database* they reach.

**The audit log doesn't record the token subject yet.** The identity is in the JWT and
that's the whole reason `subject` is there, but I haven't threaded it into `AuditEntry`.
So on HTTP the log currently answers "what happened" but not "who did it". For stdio
that's fine because there's exactly one caller; for HTTP it's the gap I'd close first.

Both are in flow.md under "next up" rather than quietly omitted.

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

## Section 5: Phase 4 — Migration engine

**Q: Which ALTERs are metadata-only vs rewrite in Postgres, and why does it matter?**
A: I verified this rather than recalling it — checking `relfilenode` before and after,
since a changed relfilenode *is* a rewrite:

```
ADD COLUMN b int NOT NULL DEFAULT 7     -> relfilenode UNCHANGED  (no rewrite)
ALTER COLUMN a TYPE bigint              -> relfilenode CHANGED    (full rewrite)
ADD COLUMN c int DEFAULT (random())     -> relfilenode CHANGED    (full rewrite)
```

The punchline is the part people miss: **all three take `AccessExclusiveLock`** — the
strictest lock Postgres has, blocking even `SELECT`. So lock *mode* tells you almost
nothing about danger. What separates a non-event from a six-minute outage is whether the
operation rewrites or scans the table *while holding* that lock. A metadata-only change
holds AccessExclusive for microseconds; a rewrite holds it for as long as copying every
row takes.

That's also why the constant-vs-volatile `DEFAULT` distinction matters so much. Those two
`ADD COLUMN` statements look nearly identical and differ by a full table rewrite —
Postgres 11 added the optimization for non-volatile defaults only. `DEFAULT 7` is free;
`DEFAULT random()` rewrites 1.2M rows.

Beyond that: `SET NOT NULL` scans but doesn't rewrite (and PG12+ can skip even the scan
if a validated `CHECK (col IS NOT NULL)` already exists); `ADD CONSTRAINT` scans and
blocks writes, which is why the `NOT VALID` → `VALIDATE CONSTRAINT` split exists —
`VALIDATE` takes only `ShareUpdateExclusiveLock` and blocks neither reads nor writes.

**Q: How do you turn that into a risk rating?**
A: Duration × *what is blocked*, not duration alone:

```python
high_threshold = 1_000 if self.blocks_reads else 5_000
```

Four seconds of AccessExclusiveLock is a user-visible outage; four seconds blocking only
writes is a slow deploy. Ranking them the same would either cry wolf about every index
build or wave through a genuine outage.

**Q: Where do the time estimates come from, and how much do you trust them?**
A: Measured, then deliberately made pessimistic. On this machine a rewrite ran at ~500k
rows/s and an index build at ~650k rows/s; the constants in the code are roughly half
that. For a safety tool the dangerous direction to be wrong in is *optimistic* — someone
told "2 seconds" who then takes two minutes of downtime on slower production storage was
actively misled by my tool.

And per ADR-004 they're never presented as guarantees. There's a test asserting that
nothing which scales with table size may claim `high` confidence, because the real rate
depends on hardware, cache state, and concurrent load that I cannot observe from outside.
Fabricating "this will take 4.2 seconds" is worse than useless — someone would plan a
maintenance window around it.

**Q: Why take a target schema as JSON instead of migration SQL?**
A: Describing desired *state* is far less error-prone for an agent than writing migration
SQL, and it lets the engine own the questions that actually matter — ordering, lock
impact, and whether a step is destructive. If the agent hands me SQL, I'm reduced to
executing whatever order it happened to pick.

Ordering is a correctness requirement, not cosmetics: Postgres rejects an index on a
column added later in the same batch. Creations go outside-in (tables → columns →
constraints → indexes) and drops strictly in reverse, because a dependency must be
created after what it depends on and dropped before it.

One subtlety worth mentioning: I normalize type aliases before comparing. Postgres
reports canonical names, so a raw string compare of `int` against `integer` would emit an
`ALTER TYPE` — a full table rewrite — for two identical types. An expensive no-op is a
bad bug for a migration tool.

**Q: What happens if a table isn't mentioned in the target?**
A: Nothing — `allow_drops` defaults to false. A target that merely omits a table is far
more likely to be a partial description than a request to destroy it, and deleting data
because something went unmentioned is exactly the failure this project exists to prevent.
The response includes a note saying what was left alone and how to opt in.

**Q: How does crash recovery work mid-migration?**
A: The ledger row is written with status `in_flight` **before** any DDL runs, then
updated to `applied` or `failed`. That ordering is the whole design. A ledger that
inserts a row on success — the obvious approach — leaves *no trace at all* of a process
killed mid-migration, so the next run cannot distinguish "never started" from "half
applied".

On startup, a row still marked `in_flight` means a crash, and `apply` refuses to proceed
rather than guessing which steps landed. There's a test that simulates exactly this by
inserting an `in_flight` row directly and asserting the next apply raises
`MIGRATION_IN_FLIGHT` and changes nothing.

The ledger also uses a *partial* unique index — `UNIQUE (migration_id) WHERE status =
'applied'` — so a migration may legitimately appear twice after a failed attempt and a
retry, but can never be applied twice.

**Q: Are migrations atomic?**
A: Usually, and I'm careful not to overclaim. Postgres has transactional DDL, so all
transactional steps run in one transaction — a failure on step 3 rolls back steps 1 and
2. There's a test that asserts exactly that: a plan whose last step references a
nonexistent type leaves none of the earlier columns behind.

The exception is `CREATE INDEX CONCURRENTLY`, which **cannot run inside a transaction
block** — verified, not assumed:

```
BEGIN; CREATE INDEX CONCURRENTLY ...;
ERROR:  CREATE INDEX CONCURRENTLY cannot run inside a transaction block
```

So a plan containing one genuinely isn't atomic, and the plan says so
(`atomic: false`) plus a note explaining that a later failure leaves earlier steps
applied. Claiming atomicity that doesn't hold would be the worst kind of wrong.

**Q: What does the dry run actually do?**
A: Executes every transactional step for real inside a transaction that is always rolled
back. That catches what static analysis cannot: a type that doesn't exist, a constraint
existing data violates, an index on a column never added. Those are precisely the
failures that would otherwise surface halfway through a real apply with earlier steps
already committed. There's a test asserting the dry run leaves no trace — if the rollback
were incomplete, `plan` would silently become `apply`.

**Q: Tell me about a bug you found in this phase.**
A: The best one: **the migration engine dropped its own ledger table.** With
`allow_drops=true` and a target that — quite reasonably — didn't mention
`pgops_migrations`, the diff decided that table was unwanted and emitted
`DROP TABLE pgops_migrations`. The migration destroyed the table recording it, then
crashed with `relation "pgops_migrations" does not exist` while trying to mark itself
finished.

I found it because a test failed with that error, which is a nice illustration of why the
integration tests run against real Postgres — no mock would have modelled a table
deleting its own bookkeeping. Fix: internal tables are excluded from the diff entirely
and refused if named as a target.

Second one, subtler: `ADD CONSTRAINT c CHECK (...)` was being classified as
`metadata_only`. My pattern for "ADD `<name>` `<type>`" — which exists because the
`COLUMN` keyword is optional in `ALTER TABLE ... ADD` — happily matched `ADD CONSTRAINT
c CHECK`, so a full-table validation scan was reported as a harmless catalog change.
Constraint clauses are now matched before the column branch. The lesson is that a
permissive fallback pattern will eventually swallow something specific, so specific cases
have to be ordered first.

**Q: You mentioned a timing bug. What was it?**
A: Every duration the project reported was wrong on Windows, and it went unnoticed for
three phases. `time.monotonic()` there is backed by the system tick counter with
**15.625 ms resolution**:

```
monotonic    resolution 0.015625 s -> measured a 10 ms sleep as   0.000 ms
perf_counter resolution 1e-07    s -> measured a 10 ms sleep as  10.470 ms
```

Most healthy operations finish inside one tick, so they were logged as `0.0`. I caught it
in the migration ledger, where a row recorded `duration_ms = 0` while its own
`started_at`/`finished_at` timestamps were 9.8 ms apart — the row disagreed with itself,
which is what made it obvious rather than merely plausible.

That's not cosmetic: `duration_ms` in the audit log is forensic data. "How long did that
`DELETE` hold its locks?" is a question an incident review asks, and `0.0` is wrong in a
way that reads as broken instrumentation. All four measurement sites now use
`perf_counter`; verified afterwards as `duration_ms 18.238` against wall-clock `18.031`.

`monotonic` is still correct for the confirmation-token TTL — a 5-minute deadline doesn't
care about 15 ms, and "has this expired" is exactly what monotonic is for.

**Q: How does migration.rollback work — and why did it come last?**
A: It came last deliberately, because "rollback" sounds like an undo button and for
schema migrations it is not one. A tool that implies reversibility will eventually be
trusted at 3am to reverse something that cannot be reversed. So the first thing the
module does is classify each recorded step into one of **three** outcomes, not two:

- *reversible* — `CREATE INDEX` → `DROP INDEX`. An index holds no information of its
  own; dropping it destroys only derived data.
- *reversible with data loss* — `ADD COLUMN` → `DROP COLUMN`. The schema reverts, but
  every value written to that column since the migration is destroyed. The confirmation
  reason says so in those words.
- *irreversible* — `DROP COLUMN`, `DROP TABLE`, a type change whose previous type wasn't
  recorded. There is no inverse; re-adding produces NULLs, not the original data.

Any irreversible step refuses the **whole** rollback. Doing the reversible half would
leave the schema in a state neither the forward migration nor the rollback describes —
worse than either during the incident that prompted this.

The most important design decision: **the irreversible refusal issues no token.** For a
risky-but-possible rollback, the token flow makes sense — a human can weigh it. For an
impossible one, minting an approval would imply there exists a version of "yes" that
restores the data, and there isn't. The refusal names the offending step and points at
restore-from-backup as the only real path back.

Two more guards worth naming:

1. **Stack check.** If later migrations were applied after this one, rollback refuses —
   reversing an earlier change underneath a later one that may depend on it produces
   failures that are hard to unwind. The refusal names the earliest blocker.
2. **Reverse order execution.** The forward plan creates a table before indexing it, so
   the inverse must drop the index before the table or the drop fails on a dependency.

Execution runs in one transaction (every reversible step here is transactional DDL by
construction — `CONCURRENTLY` steps are never classified as reversible), so a failed
rollback leaves the original migration applied, which is the only state that remains
describable. On success the ledger row becomes `rolled_back` — history stays honest
without claiming destroyed data came back.

One implementation detail that shaped the ledger: apply now records each step's
*structured* form (kind/table/target) alongside its SQL. Rollback needs the structure —
inverting SQL text by parsing it is exactly the guessing this project refuses to do. Rows
recorded without structure are refused with an explanation rather than guessed at.

## Section 6: Phase 5 — Docker layer

**Q: Isn't giving agents Docker access dangerous?**
A: Yes — genuinely, not rhetorically. **The Docker socket is root-equivalent on the
host.** Anything that can talk to it can start a privileged container with the host
filesystem mounted, and it owns the machine. That is not a Docker misconfiguration, it's
what the socket is for.

So the default posture is read-only: list, inspect, logs, stats. `container.restart` and
`container.exec` are gated twice — the server must run with `--approval-mode` AND the
call needs a confirmation token. The two gates mean different things: the flag is the
*operator* saying "this deployment may act on containers", the token is a *human*
approving this specific action. Neither alone is enough.

One design detail I'd point at: without the flag those tools aren't registered at all —
they're absent from `list_tools()`. An agent can't be tempted by, or waste turns on, a
capability it was never told exists.

**Q: You allow `container.exec`. Isn't that just remote code execution?**
A: It would be, which is why there's a third gate: a command allowlist. Only read-only
diagnostics (`ps`, `df`, `free`, `pg_isready`, `psql`, …) are permitted; a shell is
refused:

```
container.exec ['/bin/bash', '-c', 'id']  ->  EXEC_NOT_ALLOWED: 'bash' is not in the
                                              diagnostic command allowlist
```

The binary is checked by **basename**, so `/bin/bash` can't slip past a check on the
literal string `bash`. My reasoning: even behind two gates, handing an agent an arbitrary
shell is a different class of capability from container diagnostics, and an operator who
genuinely needs a shell already has `docker exec`. The tool refusing to be the most
dangerous version of itself costs almost nothing and removes a whole category of risk.

**Q: What's the most important line of code in that module?**
A: The one that isn't there. Before writing anything I probed the daemon, and
`container.attrs['Config']['Env']` came back with:

```
POSTGRES_PASSWORD=pgops_dev
```

Environment variables are where credentials live — database passwords, API keys, signing
secrets, for *every* container on the box. A topology tool that returns raw container
attributes hands all of that to the agent, into its context window, and onward into
whatever logs or transcripts that context reaches.

So `env.topology` returns an explicit **allowlist** of fields — name, image, status,
health, compose project/service, ports, mount destinations — rather than filtering a
denylist out of the raw attrs. That distinction matters: with a denylist, the next Docker
API version that adds a secret-bearing field leaks it by default. With an allowlist, new
fields are invisible until someone deliberately adds them. I also return mount
*destinations* only, since source paths leak host filesystem layout.

**Q: How do you know which container is the database?**
A: By **published host port**, matched against the DSN — not by image name. This is one
of those things where the naive version works on a clean machine and fails on a real one.
My dev box runs two Postgres containers simultaneously: mine on host port 5433 and an
unrelated project's on 5434. "Find the container whose image is postgres" would
confidently pick whichever came back first and then report *another project's* logs and
memory pressure as my database's — wrong in a way that looks authoritative.

If nothing matches, the response says so and explains why (the database may be outside
Docker, on another host, or on an unpublished port) rather than silently returning null.

**Q: Any async gotchas?**
A: One that matters. The Docker SDK is **synchronous**, and `stats(stream=False)` blocks
for about a second — it has to sample twice to compute a CPU delta, since a single
reading has no previous value to compare against. Calling that directly from an async
tool would freeze the event loop for every other in-flight request for that whole second.
Every SDK call goes through `asyncio.to_thread`.

**Q: How do you report memory?**
A: The way `docker stats` does — `usage - inactive_file` — not the raw `usage` figure.
Inactive page cache is reclaimable and not really "used". Reporting raw usage would
overstate pressure, fire the correlation hints falsely, and disagree with what the user
sees in their own terminal, which is the fastest way to lose their trust in every other
number the tool prints. There's a test asserting my figure tracks Docker's own
accounting.

**Q: What does the correlation actually do, and how do you avoid overclaiming?**
A: `env.correlate` joins `db.health` findings with the database container's stats. The
useful case is: container memory at 94% *and* a degraded buffer cache hit ratio →
"consistent with Postgres having too little memory to hold the working set".

The phrasing is the design. Every hint says "consistent with", never "the cause is",
because correlation isn't causation — a tool that states it as fact sends someone
resizing a container when the real problem was a missing index. It also stays quiet when
nothing is wrong ("no container resource pressure that would explain database symptoms").
A hint on every call is noise; silence has to carry information. There's a test asserting
the quiet case, and one asserting the hedged phrasing.

CPU throttling is the one I'd highlight as genuinely hard to spot otherwise: a container
hitting its CPU quota slows every query regardless of how well written it is, and nothing
inside Postgres will tell you that's happening.

**Q: What if Docker isn't running?**
A: This tool group degrades with a structured `DOCKER_UNAVAILABLE` error and a hint
("is Docker running, and does this user have access to the socket?"). The database tools
are entirely unaffected — that's an explicit failure mode in ARCHITECTURE.md. The test
suite skips the Docker tests rather than failing when there's no daemon, so CI on a
socketless runner stays green for everything it *can* verify.

**Q: Tell me about a test you had to fix.**
A: The secret-leak test — the most important assertion in the module — failed on a
completely **safe** response. I'd written `assert password not in output`, and the dev
password `pgops_dev` happens to be a substring of the container *name*
`pgops_dev_postgres`. The code was correct; the test was crying wolf about its own
fixture.

I rewrote it to assert on the leak's actual signature — the `KEY=value` assignment form
and the variable name — and added a positive assertion that the daemon really *is*
offering the secret, so the test can't quietly start passing because a field moved or the
fixture changed. That second part matters: a security test that passes for the wrong
reason is worse than no test.

The reason I didn't just loosen the assertion: a test that false-alarms gets weakened or
deleted by whoever hits it next, and that's how the real guarantee gets lost.
