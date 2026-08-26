# Flow — Living Progress Log

> Everything done on pgops-mcp, newest first. Updated in the same commit as any feature.
> Format: `[date] PHASE-N: what was done → why / notes`.

---

## 2026-08-26 · Phase 6f — distribution: an MCP server, not a Python library

The scope decision that shaped this phase: **pgops-mcp is a server, not a library.**
Nothing in `pgops.*` is meant to be imported, and it carries no API-stability promise.
That distinction had been blurred in the packaging metadata and the docs, and blurring
it costs something real — a `Topic :: Software Development :: Libraries :: Python
Modules` classifier advertises an import surface that does not exist, and a README whose
first instruction is `git clone` sends a *user* down a *contributor's* path.

**Important nuance, because it is easy to get backwards:** "not a Python library" does
not mean "not on PyPI". The MCP Registry hosts **metadata only** — it stores no
artifacts. Every `server.json` must point at a package in a real package registry
(PyPI / npm / NuGet / Cargo / OCI / MCPB), and `uvx pgops-mcp` — the standard install
line every MCP client understands — *is* a PyPI fetch. So PyPI stays, as the delivery
channel for an **application**; what got removed is the library framing around it.

### Three genuine distribution bugs, found by installing it the way a stranger would

Building the wheel and installing it into an empty virtualenv (rather than trusting the
dev environment, where every dev extra is already present) surfaced all three:

- **PHASE-6f · `docker` was never declared as a dependency.** `tools/environment.py`
  imports it, and six of the seventeen tools (`env.*`, `container.*`) need it. It
  reached the venv only as a transitive dependency of **testcontainers** — a *dev*
  package. A user installing from PyPI would have had every environment tool fail with
  `ImportError`. Worse, the error path said "install pgops-mcp with the docker extra"
  and **no such extra existed**, so the one message meant to rescue the user pointed at
  something imaginary. Now a hard runtime dependency: a half-working server is worse
  than an honest one.
- **PHASE-6f · `hypothesis[dev]` was a *runtime* dependency.** A property-testing
  library shipped to every user of the server, via an extra (`[dev]`) that hypothesis
  does not even publish — `uv` had been printing
  `warning: The package hypothesis==6.165.10 does not have an extra named dev` on every
  sync. Moved to the dev extra where it belongs.
- **PHASE-6f · the README was mojibake.** Every em-dash in six documents was
  double-encoded (`—` → `â€"`): the files had been read as cp1252 and written back as
  UTF-8 at some point. This is not cosmetic *here* specifically, because **the README
  becomes the PyPI project description** — it would have been the first thing anyone
  saw. Repaired with a lossless round-trip check (re-corrupting the fix must reproduce
  the original byte-for-byte) so text that was never damaged passes through untouched,
  rather than a blind search-and-replace that could mangle legitimate content.

### What shipped

- **PHASE-6f · `server.json`** — the MCP Registry manifest, declaring **two** package
  entries: `pypi` (with `runtimeHint: uvx`) and `oci`. Two, because they fail for
  different people: `uvx` needs nothing preinstalled but assumes the host may run
  Python; the container assumes only Docker. Every `PGOPS_*` variable is declared with
  `isRequired` / `isSecret` / `default`, so a client can render a real configuration
  form instead of making the user read the docs.
- **PHASE-6f · `Dockerfile`**, two-stage so the runtime image carries no build backend
  and no compiler. Non-root (uid 10001) by default. `VOLUME /var/lib/pgops` because
  **an audit log that dies with the container is not an audit log** — declaring the
  volume makes that explicit rather than leaving it to be discovered after an incident.
  Carries `LABEL io.modelcontextprotocol.server.name`, which is how the registry
  verifies OCI ownership.
- **PHASE-6f · registry ownership markers.** PyPI ownership is proven by an
  `mcp-name: io.github.arzharch/pgops-mcp` marker in the README (which becomes the PyPI
  description); OCI ownership by the image label above. Both are asserted in CI, because
  losing the marker during an ordinary docs edit would break publishing in a way that is
  baffling after the fact.
- **PHASE-6f · `publish.yml`**, tag-gated. Job order is load-bearing, not tidy:
  `verify → (pypi, container) → registry`. The registry validates that the artifact it
  points at already exists and is provably ours, so publishing metadata first simply
  fails. PyPI upload uses **Trusted Publishing** (OIDC), so no long-lived API token has
  to exist as a repository secret at all. The verify job cross-checks the version in
  `pyproject.toml`, `pgops.__version__`, `server.json` *and* its `packages[]` entry
  against the tag — caught before PyPI accepts an immutable version number, not halfway
  through a release.
- **PHASE-6f · `CONTRIBUTING.md`**, so `git clone` + `uv sync` lives where contributors
  look and no longer greets users on the front page.

### One flaky-test bug, worth recording

- **PHASE-6f · two suites both hardcoded port 8795.** `test_live_server` and
  `test_audit_identity` (the latter added earlier this session) each booted a real HTTP
  server on the same fixed port. Each passed in isolation and the pair failed in the
  full run — one either failed to bind or, worse, connected to the *other* suite's
  server and asserted about it. The visible symptom was a cascade of fifteen unrelated
  errors in `test_redteam`, which runs after them, so the failure pointed at the wrong
  file entirely.

  Passing alone and failing together is the signature of a shared-resource collision
  rather than a real defect, which is what made it findable. Fixed with a `free_port()`
  helper in conftest that binds port 0 and lets the OS choose — applied to all five
  suites that boot a server, since renumbering would have left the same trap for the
  next one. Function-scoped fixtures made it worse: the red-team suite rebinds per test,
  fifteen times in a row, which on Windows can land in TIME_WAIT.

### Gate evidence

- Clean-room install from the built wheel into an empty venv: `pgops-mcp --selfcheck`
  connected to the 1.2M-row dev database and listed all 4 tables. Asserted that
  `hypothesis`, `pytest` and `testcontainers` are **absent** from that environment.
- Container: builds, runs `--selfcheck` successfully against the host database via
  `host.docker.internal`, `id` confirms uid=10001 (non-root), and
  `docker inspect` shows the `io.modelcontextprotocol.server.name` label.
- 436 tests pass; ruff and mypy clean.
- Release preconditions validated locally: all four version strings agree, README marker
  present, `server.json` name matches. Both workflow files parse as YAML — which caught
  a real break: an unquoted `mcp-name: ...` inside a `run:` value is invalid YAML
  (`mapping values are not allowed here`), so the publish workflow would have failed on
  its first run.

---

## 2026-08-25 · Phase 6d — observability: traces, metrics, health

The difference between "has an audit log" and "is observable". The audit log answers
*who did what, approved by whom* (forensic); this layer answers *how slow, how often,
how healthy* (operational). Both are needed before "production-grade" is an honest claim.

- **PHASE-6d · one design constraint above all: telemetry must never break the
  operation it describes.** With `PGOPS_OTEL_ENDPOINT` unset every call in
  `observability.py` is a cheap no-op — asserted by test, not by intention. A
  monitoring layer that can fail the request it monitors is worse than no monitoring.
  The otel/aiohttp dependencies are an optional extra (`uv sync --extra otel`); the
  module imports them lazily and degrades with a warning if absent.
- **PHASE-6d · spans at the boundary, not per tool.** `tool_boundary` now opens a
  `ToolSpan`, so all 15 tools are covered by construction rather than by remembering to
  instrument each one. Spans carry what an incident responder needs: tool name,
  verdict (`executed` / `refused` / `failed`), error code, duration. Refusals are
  spans too — a spike in CONFIRMATION_REQUIRED is itself an operational signal (an
  agent trying something it shouldn't).
- **PHASE-6d · four metrics, chosen for a service like this:** `pgops.tool.calls`
  (counter by tool + verdict), `pgops.tool.duration` (histogram for p99 SLOs),
  `pgops.pool.timeouts` (pool saturation), `pgops.db.up` (gauge from healthcheck).
  Metrics are recorded even when no span exists — they're the cheaper signal and the
  one dashboards are built on.
- **PHASE-6d · liveness vs readiness, the distinction operators actually need.**
  `/health` says the process is alive; `/ready` says it can do its job right now
  (Postgres reachable). Liveness failing → restart; readiness failing → stop sending
  traffic but don't restart — the database being down is not this process's fault.
  Served on `PGOPS_HEALTH_PORT` via aiohttp, started alongside any transport.
- **PHASE-6d · failure masking preserved under instrumentation.** The boundary still
  converts unexpected exceptions into structured INTERNAL_ERROR without leaking
  internals — verified by test that the wrapping itself doesn't change refusal or
  masking semantics.

### Gate evidence

```
uv run pytest -q -m "not slow"   # 369 passed + 9 deselected (7 new observability tests)
uv run ruff check .              # All checks passed!
uv run mypy src                  # Success: no issues found in 33 source files
```

Live verification against a local Jaeger (`docker run --rm -p 4317:4317 -p 16686:16686
jaegertracing/all-in-one`) with the server on HTTP transport:

```
query.read over the wire   -> span tool.query_read_tool, verdict=executed, real durations
bad column (PgopsError)    -> verdict=refused, pgops.error_code=INVALID_ARGUMENT
docker stop postgres       -> /ready 503 while /health stays alive; recovers after start
Jaeger down                -> export warnings in log; every tool call still succeeds
```

One gap found by that live pass and closed immediately: **scope denials were invisible.**
`ScopeEnforcement` runs before the tool body, so a denied call produced a log line but no
span and no metric — despite being one of the most operationally interesting signals
(misconfigured agent, rotated token missing scopes, probing). Fixed in the same phase:

- **PHASE-6d · ObservabilityMiddleware, registered outermost.** Wraps every tool call
  including the scope check. Verdict taxonomy is deliberately four values: `executed`,
  `refused` (the tool said no), `denied` (authorization said no before the tool ran),
  `failed` (unexpected exception). "The tool refused" and "you may not ask" are different
  incidents with different responders.
- **PHASE-6d · metrics emitted once per call.** Only the boundary's span emits
  `pgops.tool.calls`/`pgops.tool.duration`; the middleware span is span-only for calls
  that reach the tool, and emits the metric itself only for denials — nested spans
  double-counting traffic would be worse than no metrics because it lies about volume.
  The counter now also carries a `caller` dimension (token subject or `local`).

---

## 2026-08-25 · Phase 6e — live-server evals (and a real security fix)

"I made it and I'm happy with it" is not evidence. This phase adds an evaluation suite
that boots the real server — HTTP transport, JWT auth, scope enforcement, observability
middleware — and drives it with a real MCP client over the wire, asserting the claims an
operator would actually depend on. Marker: `live` (`uv run pytest tests/test_live_server.py -m live`).

- **PHASE-6e · the full verdict taxonomy is reachable end-to-end.** executed / denied /
  refused / failed all asserted through real HTTP, because if any outcome were
  unreachable or mislabeled through the deployed stack, every dashboard and alert built
  on Phase 6d would be silently wrong.
- **PHASE-6e · safety guarantees survive the network hop.** Unbounded DELETE refused,
  token binding holds, single-use holds, rows untouched while gated — evaluated as a
  service, not as functions.
- **PHASE-6e · benchmarks with p95 budgets, not vibes.** query.read p95 ≤ 500ms,
  db.health ≤ 250ms, denial fast-path ≤ 200ms (denials touch no database; if they cost
  as much as reads, middleware grew a hidden pool dependency), plus a concurrency check
  that 10 parallel reads don't serialize toward 10x latency.
- **PHASE-6e · the suite found a real bug on its first run.** A confirmation token
  issued for a *refused* statement (unbounded DELETE) could be redeemed on any *allowed*
  statement: the redeem check only ran inside the refusal branch, so an allowed
  statement carrying someone else's token executed without ever checking what that token
  approved. In-process unit tests missed it because they only tested one direction.
  Fixed in `query_write`: supplied tokens are redeemed unconditionally before any other
  logic; a misapplied credential raises CONFIRMATION_MISMATCH and leaves the original
  approval intact. Regression tests added in both directions.

### Gate evidence

```
uv run pytest -q -m "not live and not slow"   # 372 passed
uv run pytest tests/test_live_server.py -m live  # 8 passed, twice consecutively
BENCH query.read(count)   n=20  p50=28.9ms   p95=33.9ms   (budget 500ms)
BENCH db.health           n=20  p50=27.6ms   p95=34.1ms   (budget 250ms)
BENCH denied(write)       n=20  p50=15.9ms   p95=20.5ms   (budget 200ms)
BENCH concurrent(10 reads)      batch=454ms                (no serialization)
uv run ruff check .                            # All checks passed!
uv run mypy src                                # Success: no issues found in 33 source files
```

---

The last tool from FR-3, and deliberately the last: "rollback" sounds like an undo
button, and for schema migrations it is not one. The naive version is dangerous in a way
that is easy to miss — a tool that implies reversibility will eventually be trusted at
3am to reverse something that cannot be reversed.

- **PHASE-4c · three outcomes, not two.** Each recorded step is classified:
  *reversible* (`CREATE INDEX` → `DROP INDEX` — an index is derived data),
  *reversible-with-data-loss* (`ADD COLUMN` → `DROP COLUMN` — schema reverts, every
  value written since is destroyed), *irreversible* (`DROP COLUMN`, `DROP TABLE`,
  type changes with no recorded previous type). **Any irreversible step refuses the
  whole rollback** — doing the reversible half would leave the schema in a state
  neither the migration nor the rollback describes.
- **PHASE-4c · the refusal issues no token.** Unlike a risky-but-possible rollback,
  there is no version of "yes" that makes dropped data come back, so minting an approval
  would imply one exists. The refusal names the offending step and points at
  restore-from-backup as the only real path.
- **PHASE-4c · structured step records in the ledger.** `migration.apply` now writes
  each step's kind/table/target alongside its SQL. Rollback needs the structure:
  inverting SQL text by parsing it is exactly the guessing this project refuses to do.
  Rows recorded without structure are refused with an explanation rather than guessed at.
- **PHASE-4c · stack check.** A migration with later applied migrations on top is
  refused — reversing an earlier change under a later one that may depend on it produces
  failures that are hard to unwind. Refusing names the blocker instead of discovering it
  mid-rollback.
- **PHASE-4c · execution is transactional and gated like apply.** All reversals here are
  transactional DDL by construction (CONCURRENTLY steps are never recorded as
  reversible), so all-or-nothing holds; a failed rollback leaves the original migration
  applied, which is the only state that remains describable. Confirmation token bound to
  migration id + checksum, single-use, audited on refusal and execution alike. On
  success the ledger row becomes `rolled_back`.
- **PHASE-4c · scope:** `pgops:write` — same tier as `migration.apply`; a rollback can
  destroy data just as an apply can.

### Gate evidence

```
uv run pytest -q      # 371 passed (15 new rollback tests)
uv run ruff check .   # All checks passed!
uv run mypy src       # Success: no issues found in 32 source files
```

Live behaviour verified against testcontainers Postgres:

```
ADD COLUMN tag -> applied -> rollback (token) -> column gone, ledger row = rolled_back
CREATE INDEX   -> applied -> rollback -> index gone, note: "no data was destroyed"
plan w/ DROP COLUMN -> applied -> rollback -> MIGRATION_IRREVERSIBLE, no token issued
two stacked applies -> rollback of first -> refused: "1 migration(s) were applied after"
sabotaged reversal  -> MIGRATION_FAILED, transaction rolled back, status still 'applied'
```

---

## 2026-08-25 · Phase 6a — MCP protocol completeness

Audit prompted by a direct question: the server had shipped **tools only**, which is one
of three server primitives. Everything below closes that gap.

- **PHASE-6a · resources.** Tools are model-controlled and cost a turn; resources are
  *application*-controlled and can be attached as context by the client without the
  model deciding to spend one. "What does my schema look like" is background context for
  almost every database conversation, so it belongs in a resource. Added
  `pgops://schema`, `pgops://schema/summary`, `pgops://schema/{table}` (template),
  `pgops://health`, `pgops://migrations`, `pgops://audit/recent`, `pgops://config`.
  All read-only and mirroring existing tools, so no new capability and no new attack
  surface.
- **PHASE-6a · two resources needed deliberate redaction.** `pgops://config` omits the
  DSN (it contains the password). `pgops://audit/recent` returns verdicts, timings and
  SQL *hashes* but **not statement text** — the on-disk log keeps full SQL because an
  incident review needs it, but a resource may be auto-attached to model context, and
  executed SQL embeds literal values (an email in a WHERE clause, an amount in an
  UPDATE). Tested.
- **PHASE-6a · prompts.** Five workflows (`diagnose-slow-query`, `plan-safe-migration`,
  `incident-triage`, `review-index-health`, `explain-safety-model`). Prompts are the
  right home for the thing no individual tool can express: *the order to use the tools
  in and what to do with the answers*. Without them that judgment lives only in whatever
  the user types and gets re-derived, differently, every session. Each one steers toward
  the safe path (check `stats_window` before recommending a DROP; never call
  `migration.apply` before the user has seen the lock impact).
- **PHASE-6a · elicitation — the most significant gap.** The confirmation-token protocol
  has a structural weakness: approval round-trips **through the agent**. The server
  hands back a token and trusts the model to relay the reason honestly and only return
  once a human agreed. Nothing enforced the middle step. Elicitation asks the *user*
  directly, outside the model's turn, so the model cannot fabricate the answer.
  - Policy: elicit when the client supports it, fall back to tokens when it does not —
    and **never** fall back to "allowed". Losing elicitation degrades approval from
    "the human was asked" to "the agent asserts the human was asked".
  - A human "no" raises `CONFIRMATION_DECLINED` and issues **no token**: an explicit
    refusal must not be convertible into a credential the agent redeems seconds later.
  - The audit log records *which* method approved each action, because "the human was
    asked" and "the agent presented a token" are different assurances.
- **PHASE-6a · a deprecation warning that was a real client bug.** `ctx.elicit()`
  without a `response_type` produces an empty schema that "causes some clients (e.g.
  VS Code) to render an empty, non-functional form" — the user is asked to approve
  something with no way to answer. VS Code is a target client per the PRD. Fixed by
  sending an explicit `["approve", "cancel"]` choice. Also handled the case where the
  client accepts the *prompt* but the user picks "cancel" — treating the envelope as the
  answer would approve everything actively declined.
- **PHASE-6a · progress and client logging** helpers, both best-effort: telemetry must
  never break the operation it describes.

## 2026-08-25 · Phase 6b — HTTP transport and agent auth

- **PHASE-6b:** Auth is bound to the **transport**, not offered as a global flag. Over
  stdio there is no remote caller to authenticate and requiring a token would be theatre
  (ADR-002 — this is why Phases 1–5 shipped with none). Over HTTP the port is reachable,
  so `--transport http` **refuses to start without `--public-key`**.
- **PHASE-6b:** RS256, not a shared secret. The server holds only the public key, so a
  server compromise leaks the ability to *verify* tokens, never to *issue* them.
- **PHASE-6b:** Scopes map to the danger tiers the project already uses —
  `pgops:read` / `pgops:write` / `pgops:admin` — so a token can be genuinely incapable
  of the thing you are worried about. New tokens default to **read-only**. Tools with no
  scope entry require `admin`: deny-by-default, same principle as the SQL classifier.
  `migration.plan` is read (its dry run is rolled back); `migration.apply` is write.
- **PHASE-6b:** `subject` lands in the token and identifies the agent. With stdio there
  is one caller and identity is implicit; an HTTP server has many, and "who ran this
  DELETE" has to be answerable.
- **PHASE-6b:** HTTP binds `127.0.0.1` by default — `0.0.0.0` would expose a database
  operator to the whole network the moment someone tried HTTP mode.
- **PHASE-6b:** Key management ships in the same binary: `pgops-mcp keygen`,
  `pgops-mcp issue-token --subject <agent> --scope <scope>`, `pgops-mcp scopes`.

### Gate evidence

```
uv run pytest -q      # 319 passed
uv run ruff check .   # All checks passed!
uv run mypy src       # Success: no issues found in 28 source files
```

Live HTTP server with auth, against the 1.2M-row dev database:

```
=== no token ===     rejected: 401 Unauthorized
=== bad token ===    rejected: 401 Unauthorized
=== valid token ===  authenticated, tools: 13
                     query.read over HTTP -> [{'n': 1200000}]
```

---

## 2026-08-24 · Phase 5 — Docker environment layer

Two concerns drove this phase, neither of them about Docker features.

- **PHASE-5 · security: container metadata is full of secrets.** Probing the dev stack
  before writing any code, `container.attrs['Config']['Env']` returned
  `POSTGRES_PASSWORD=pgops_dev` verbatim — and on a real machine that field holds API
  keys and signing secrets for every other container. A topology tool that returns raw
  attributes hands all of it to the agent, into its context window, and onward into
  whatever logs that context reaches. `env.topology` therefore builds an **allowlist**
  of fields to return rather than filtering a denylist out of the raw attrs: with a
  denylist, the next Docker API version that adds a secret-bearing field leaks it by
  default. Mount *destinations* only are returned; source paths leak host layout.
- **PHASE-5 · security: the Docker socket is root-equivalent.** Anything that can talk
  to it can mount the host filesystem into a privileged container. Default posture is
  read-only (list/inspect/logs/stats). `container.restart` and `container.exec` are
  gated twice — the server must run with `--approval-mode` AND the call needs a
  confirmation token — and are not even *registered* as tools without the flag, so an
  agent is never told they exist.
- **PHASE-5 · `container.exec` is a third gate: a command allowlist.** Even behind two
  gates, an arbitrary shell is a different class of capability from container
  diagnostics. Only read-only diagnostic commands (`ps`, `df`, `pg_isready`, …) are
  permitted; the binary is checked by basename so `/bin/bash` cannot slip past a name
  check. An operator who genuinely needs a shell already has `docker exec`.
- **PHASE-5:** DSN→container matching is by **published host port**, not image name.
  This machine runs several Postgres containers at once — ours on 5435 and other
  projects' on 5432/5433/5434. Matching on "the image is postgres" would confidently pick the
  wrong one and then report another project's logs as our database's.
- **PHASE-5:** Every Docker SDK call goes through `asyncio.to_thread`. The SDK is
  synchronous and `stats(stream=False)` blocks for ~1 second (it samples twice to
  compute a CPU delta), which would otherwise freeze the event loop for every other
  request.
- **PHASE-5:** Memory is reported the way `docker stats` computes it — usage minus
  `inactive_file` — because inactive page cache is reclaimable and not really "used".
  Reporting the raw figure would overstate pressure, fire the correlation hints falsely,
  and disagree with what the user sees in their own terminal.
- **PHASE-5:** `env.correlate` joins `db.health` findings with container stats and is
  deliberately narrow. Hints are phrased "consistent with", never as diagnoses:
  correlation between a cache-hit dip and memory pressure is suggestive, and a tool
  that states it as fact sends someone resizing a container when the real cause was a
  missing index. It also stays quiet when nothing is wrong — a hint on every call is
  noise, and silence has to mean something.
- **PHASE-5:** Docker being unavailable degrades this tool group only, with a
  structured `DOCKER_UNAVAILABLE` error; the database tools are unaffected
  (ARCHITECTURE.md failure modes).

### Test-quality note

The secret-leak test initially failed on a **safe** response: the dev password
`pgops_dev` is a substring of the container *name* `pgops_dev_postgres`, so a naive
`password not in output` check cried wolf about its own fixture. Rewritten to assert on
the leak's actual signature (the `KEY=value` assignment form), plus a positive assertion
that the daemon really is offering the secret — so the test cannot quietly start passing
because a field moved. A test that false-alarms gets weakened or deleted later, which is
how the real guarantee gets lost.

### Gate evidence

```
uv run pytest -q      # 268 passed
uv run ruff check .   # All checks passed!
uv run mypy src       # Success: no issues found in 24 source files
```

Live dev compose stack:

```
dsn_host_port: 5435
database_container: pgops_dev_postgres | health: healthy
compose_projects: {'pgops-mcp': 1, 'appointment': 1}
postgres containers seen: ['pgops_dev_postgres', 'appointment-langfuse-db-1']   <- picked the right one
stats: cpu 0.0%  memory 176283648 / 3950202880 (4.46%)
logs: scanned 89, returned 48 at min_severity=LOG
correlate: "no container resource pressure that would explain database symptoms"
```

Gates, verified end to end:

```
without --approval-mode:  container.restart / container.exec are NOT registered at all
with    --approval-mode:  restart      -> CONFIRMATION_REQUIRED ("drops every open connection")
                          token reuse  -> CONFIRMATION_MISMATCH (bound to one container)
                          exec /bin/bash -> EXEC_NOT_ALLOWED ('bash' not in allowlist)
                          exec pg_isready + token -> exit 0, "accepting connections"
```

---

## 2026-08-24 · Phase 4 — Migration engine ⭐

Built against verified Postgres 16 behaviour, not recalled documentation. Probes run
first, then the code written to match:

```
CREATE INDEX CONCURRENTLY inside BEGIN  -> ERROR: cannot run inside a transaction block
ADD COLUMN b int NOT NULL DEFAULT 7     -> relfilenode UNCHANGED  (no rewrite)
ALTER COLUMN a TYPE bigint              -> relfilenode CHANGED    (full rewrite)
ADD COLUMN c int DEFAULT (random())     -> relfilenode CHANGED    (full rewrite)
```

- **PHASE-4:** `migrations/lock_analysis.py` — the differentiator. The key insight the
  module is built on: **all three ALTERs above take `AccessExclusiveLock`, so lock mode
  alone tells you almost nothing.** What separates a non-event from an outage is whether
  the operation *rewrites or scans* while holding it. Hence the constant-vs-volatile
  DEFAULT split — near-identical SQL, differing by a full table rewrite.
- **PHASE-4:** Risk is duration × *what is blocked*, not duration alone: the "high"
  threshold is 1s when reads are blocked and 5s when only writes are. Ranking them the
  same would either cry wolf about index builds or wave through a real outage.
- **PHASE-4:** Estimate rates calibrated by measurement (rewrite ~500k rows/s, index
  build ~650k rows/s on this machine) then **halved** — for a safety tool the dangerous
  direction to be wrong in is optimistic. Nothing that scales with table size may claim
  `high` confidence, since the rate depends on hardware we cannot see (ADR-004).
- **PHASE-4:** `migrations/diff.py` — target schema as JSON, not DDL: describing desired
  state is far less error-prone for an agent than writing migration SQL, and it lets the
  engine own ordering. Creations run outside-in (tables → columns → constraints →
  indexes), drops strictly in reverse. Type aliases are normalized (`int`/`integer`,
  `varchar`/`character varying`) because a raw string compare would emit a spurious
  `ALTER TYPE` — a full rewrite — for two identical types.
- **PHASE-4:** `allow_drops` defaults to **false**. A target that merely omits a table is
  far more likely to be a partial description than a request to destroy it.
- **PHASE-4:** `migrations/ledger.py` — the row is written `in_flight` **before** the DDL
  runs, not after. A ledger that inserts on success leaves no trace of a process killed
  mid-migration, and cannot distinguish "never started" from "half applied". A partial
  unique index allows retry-after-failure while making double-apply impossible.
- **PHASE-4:** `tools/migrations.py` — `plan` dry-runs every transactional step inside a
  doomed transaction, catching what static analysis cannot (a type that doesn't exist, a
  constraint existing data violates). Non-transactional steps (`CONCURRENTLY`) are
  reported as making the plan **not atomic** rather than letting the caller assume a
  guarantee that doesn't hold.

### Bugs found and fixed during Phase 4

- **The engine dropped its own ledger.** With `allow_drops=true` and a target that
  (reasonably) didn't mention it, the diff emitted `DROP TABLE pgops_migrations` — the
  migration destroyed the table recording it, then crashed with
  `relation "pgops_migrations" does not exist` while marking itself finished. Internal
  tables are now excluded from the diff and refused as a target.
- **`ADD CONSTRAINT` was misreported as a harmless column add.** The permissive
  "ADD `<name>` `<type>`" pattern (needed because the `COLUMN` keyword is optional)
  swallowed `ADD CONSTRAINT c CHECK (...)`, hiding a full-table validation scan behind a
  `metadata_only` verdict. Constraint clauses are now matched first.
- **Every duration this project reports was wrong on Windows.** `time.monotonic()` has
  15.625 ms resolution there — it measures a 10 ms sleep as `0.000 ms`. Caught in the
  ledger, which stored `duration_ms = 0` for a migration whose own timestamps were
  9.8 ms apart: the row disagreed with itself. `duration_ms` is forensic data in the
  audit log, so this was a real defect, not cosmetic. All four measurement sites now use
  `perf_counter` via `timing.py`; verified `duration_ms 18.238` against wall `18.031`.

### Gate evidence

```
uv run pytest -q      # 243 passed
uv run ruff check .   # All checks passed!
uv run mypy src       # Success: no issues found in 23 source files
```

SPEC gate — "add column + change type on a large table: plan flags the type change as
high-lock-risk with reasoning and suggests the safe multi-step pattern". On the live
1.2M-row `orders`:

```
plan_id=beRpo3j8CtAxNKpr atomic=True highest_risk=high dry_run_ok=True

  ALTER TABLE "orders" ADD COLUMN "note" text
    op=metadata_only  risk=low   estimate=1ms     confidence=high
    why: PG11+ does not rewrite the table for a non-volatile default...

  ALTER TABLE "orders" ALTER COLUMN "total_cents" TYPE bigint
    op=table_rewrite  risk=high  estimate=4800ms  confidence=medium
    why: rewrites every row and rebuilds every index, holding AccessExclusiveLock...
    SAFER: Add a new column of the target type, backfill in batches, sync with a
           trigger, swap the names, then drop the old column.
```

Apply / ledger / gating verified live: safe change applied and recorded; a 7-step
destructive plan refused with `CONFIRMATION_REQUIRED` naming the actual data loss
(`orders.customer_id holds data that a down-migration cannot restore`); 1,200,000 rows
untouched after refusal; ledger history clean with `in_flight: []`.

---

## 2026-08-24 · Phase 3 — Explain & performance brain

- **PHASE-3:** `plan_analysis.py` — pure functions over `EXPLAIN (FORMAT JSON)` output,
  no DB access, so every rule is unit-testable against captured plans. Six verdict
  kinds: `seq_scan_large_table`, `expensive_filter`, `estimate_divergence`,
  `sort_spill`, `nested_loop_blowup`, `dominant_node`.
- **PHASE-3:** Two correctness details that decide whether the output is trustworthy:
  - **Loops.** `Actual Rows` / `Actual Total Time` / `Plan Rows` are all *per loop*.
    A node reporting `Actual Rows: 80000, Actual Loops: 3` returned 240,000 rows. A
    parser that compares `Plan Rows` to `Actual Rows` directly reports wild estimate
    divergence on every parallel plan in existence.
  - **Parallel loops ≠ sequential loops.** Got this wrong first and the dev database
    caught it: the analyzer emitted *"5180ms of 2400ms total (216%)"*. Under a Nested
    Loop, `Actual Loops` counts iterations; under a Gather it counts concurrent
    *workers*. Rows sum across workers, wall-clock time does not. Nodes now carry a
    `parallel` flag set while descending through Gather/Gather Merge; time multiplies
    by loops only when it isn't set. Same query now reads 876ms of 1248ms (70%).
- **PHASE-3:** Self time, not total time, identifies the bottleneck — a node's
  `Actual Total Time` includes its children, so ranking by total always names the root.
- **PHASE-3:** Thresholds are named constants with stated reasoning (`SEQ_SCAN_MIN_ROWS
  = 10_000` — below that a seq scan is the *correct* plan and flagging it would train
  the reader to ignore the verdict list). Several tests assert healthy queries produce
  **no** verdict.
- **PHASE-3:** `tools/explain.py` — `query.explain`. The safety issue unique to this
  tool: **`EXPLAIN ANALYZE DELETE FROM orders` performs the delete.** `analyze=false`
  (default) never executes. `analyze=true` on a mutating statement runs inside a
  transaction that is *always* rolled back, and still goes through the full
  guardrail + confirmation-token + audit path — rollback is not a complete undo
  (`nextval()` and side effects inside functions don't roll back), so the gate stays
  rather than being waived on the strength of "we roll it back anyway". The rollback is
  structural: the transaction block is exited by raising, so no code path commits.
- **PHASE-3:** `tools/advisor.py` — `index.advise`. Findings ordered by confidence:
  unused indexes and redundant (prefix) indexes from catalog structure, sequential-scan
  hotspots and slowest statements from workload stats. Primary-key and unique indexes
  are never reported as unused or redundant — they enforce correctness, and "nobody
  scanned it" is not a reason to drop a uniqueness guarantee. Degrades gracefully with
  an explanatory note when `pg_stat_statements` isn't installed.
- **PHASE-3 · BUG found on the live stack:** the advisor recommended
  `DROP INDEX idx_orders_customer_id` — an index a query had used *seconds earlier*.
  `pg_stat` counters lag, so it read a stale `idx_scan = 0`. Confidently wrong advice
  is worse than none: it discredits every other finding. Now the observation window is
  measured from `pg_stat_database.stats_reset`, reported in the response, and gates the
  recommendation — under 7 days the finding is marked `confidence: low` and the
  suggestion becomes an explicit "do NOT drop this yet".

### Gate evidence

```
uv run pytest -q      # 163 passed
uv run ruff check .   # All checks passed!
uv run mypy src       # Success: no issues found in 17 source files
```

SPEC gate requires ≥10 seeded slow-query scenarios diagnosed correctly —
`tests/test_explain.py` has 18 against real Postgres (seq scan, expensive filter, sort
spill, nested loop, estimate divergence from correlated columns, plus negative cases:
indexed lookup, PK lookup, and small-table scan must produce *no* verdict).

Live dev stack (1.2M-row `orders`):

```
query.explain "SELECT * FROM orders WHERE status='paid' ORDER BY total_cents" analyze=true
  [warning] sort_spill           sort spilled to disk (external merge, 3,712 kB)
  [warning] seq_scan_large_table sequential scan examined 1,200,000 rows on orders
  [info]    dominant_node        876ms of 1248ms total (70%) in this node alone

index.advise
  hotspot orders: 21 sequential scans read 8,400,000 rows from a table of ~1,200,000
  stats_window: {'observed_for': 'unknown', 'sufficient_for_unused_index_advice': False}
```

---

## 2026-08-24 · Phase 2 — Write path + safety architecture

- **PHASE-2:** `guardrails.py`: two independent mechanisms. (1) Unbounded-mutation
  detection — UPDATE/DELETE with no WHERE is refused. Detected from the sqlparse token
  stream, not `"where" in sql.lower()`, which would be fooled by
  `INSERT INTO log VALUES ('where')`, a column named `wherefore`, and
  `DELETE FROM orders -- WHERE id = 1` (all three are test cases). (2) Confirmation
  tokens: destructive/unknown statements are refused on first call and return a token.
- **PHASE-2:** Token binding is the non-obvious part. A token that only means "the user
  approved something" is forgeable-by-confusion — an agent could get approval for
  `DELETE FROM staging` and redeem it against `DELETE FROM orders`. Each token is bound
  to the SHA-256 of the exact statement it was issued for; redeeming against different
  SQL fails with `CONFIRMATION_MISMATCH` and — deliberately — does *not* consume the
  token, since the user's real pending approval is still legitimate. Tokens are
  single-use, TTL-bound (5 min default), minted with `secrets.token_urlsafe` (it's an
  authorization credential, not a random id), and in-memory only so a restart
  invalidates every outstanding approval.
- **PHASE-2:** `audit.py`: append-only JSONL at `~/.pgops/audit.jsonl` (home, not CWD —
  an MCP server is launched by the client with a working directory the user never
  chose). Every executed statement *and every refusal* is recorded: a blocked
  `DELETE FROM orders` is precisely the event an incident review needs, and a naive
  "log what we ran" design discards it. SQL stored with its SHA-256 so identical
  statements group without string-matching over embedded literals. An audit write
  failure logs loudly but never fails the tool call that already succeeded.
- **PHASE-2:** `tools/write.py` — `query.write`, ordered classify → guardrails → token →
  execute → audit. The token check sits *after* guardrail evaluation so a token can only
  unblock an already-identified specific risk; it is not a general "skip safety" flag.
  Executes inside an explicit transaction with `SET LOCAL statement_timeout`, so a
  cancelled statement rolls back rather than half-applying.
- **PHASE-2:** `--read-only` removes `query.write` from the advertised tool list
  entirely rather than registering it and refusing at call time — an agent cannot be
  tempted by a tool it was never told exists.

### Gate evidence

```
uv run pytest -q      # 115 passed
uv run ruff check .   # All checks passed!
uv run mypy src       # Success: no issues found in 14 source files
```

Against the live dev stack (real 1.2M-row `orders` table), driven through the actual
FastMCP server object:

```
orders before: 1200000
REFUSED: CONFIRMATION_REQUIRED | DELETE has no WHERE clause and would affect every row in the table
orders after refusal: 1200000          <- nothing deleted
TOKEN REUSE ON DIFFERENT SQL: CONFIRMATION_MISMATCH
BOUNDED UPDATE: {'rows_affected': 3, 'duration_ms': 110.0, 'audit_id': '...', 'classification': 'write'}
```

Resulting audit trail — refusal, blocked token-reuse, and execution all captured:

```json
{"verdict":"refused_pending_confirmation","sql":"DELETE FROM orders","detail":"DELETE has no WHERE clause and would affect every row in the table"}
{"verdict":"refused_bad_token","sql":"DROP TABLE orders","error_code":"CONFIRMATION_MISMATCH"}
{"verdict":"executed","sql":"UPDATE orders SET status = 'paid' WHERE id <= 3","rows_affected":3,"duration_ms":110.0}
```

---

## 2026-08-24 · Phase 1 review — production hardening

Audit of the Phase 1 code before starting Phase 2. Found four real defects, all with
regression tests now:

- **BUG (transport):** `schema.inspect(level="full")` was broken for every caller.
  `pg_constraint.contype` is Postgres's internal `"char"` type, which asyncpg decodes to
  Python `bytes` — `json.dumps` cannot encode it, so the tool failed at the MCP
  serialization boundary. It went unnoticed because the selfcheck and manual testing
  both only exercised `level="summary"`. Fixed by expanding `contype` to a readable
  label in SQL (`primary_key`, `foreign_key`, …) and routing all catalog output through
  `serialize_value`. `db.health` had the same latent defect (`dead_pct` is a numeric →
  `Decimal`), and its JSON test passed only because a freshly seeded container has no
  dead tuples — the test now generates churn first.
- **BUG (error leakage):** `schema.inspect` passed the table name into `$1::regclass`,
  which parses its input as an identifier expression — any name needing quoting
  (`"Order Items"`) raised a raw `InvalidNameError` that escaped the tool layer with a
  traceback, violating SPEC cross-cutting rule #2. Fixed by keying every catalog query
  off `pg_class.oid`, removing name parsing entirely.
- **BUG (error leakage, systemic):** each tool caught only `PgopsError`, which by
  definition catches only anticipated failures. Added `tool_boundary` — a decorator
  wrapping every tool that catches `Exception`, logs the full traceback to stderr, and
  returns a generic `INTERNAL_ERROR` to the caller.
- **BUG (liveness):** `pool.acquire()` has no default timeout — with every connection
  held by slow queries, further tool calls hung indefinitely with no error. Added
  `ConnectionManager.acquire_readonly()` with a bounded wait surfacing
  `POOL_EXHAUSTED`.

Quality improvements in the same pass:

- **N+1 removed:** `schema.inspect` ran 3 catalog queries *per table* (600 round trips
  for a 200-table schema at `level="full"`). Now 3 total via `= ANY($1::oid[])`.
- **Lock detection corrected:** replaced the hand-rolled `pg_locks` self-join with
  `pg_blocking_pids()`. The self-join is the version that circulates on blogs and is
  subtly wrong — it misses lock types not identified by locktype/relation (tuple,
  transactionid, advisory) and reports false positives for non-conflicting modes.
- **Logging to stderr, explicitly:** under stdio transport stdout *is* the MCP protocol
  channel; one stray log line corrupts the session in a way that looks like a client bug.
- **Tool names match docs:** were `schema_inspect_tool` etc. (derived from Python
  function names); now `schema.inspect`, `query.read`, `db.health` per TOOLS.md. A tool
  name is a public contract, not an implementation detail.
- **Test isolation:** the fixture used `TRUNCATE items` without `RESTART IDENTITY`, so
  re-seeded rows continued from the previous test's id high-water mark and assertions
  like `WHERE id <= 5` matched a different number of rows depending on test order.
- Also added: `py.typed` marker, config-resolution tests, and end-to-end tests through
  the real FastMCP server — the layer where both serialization bugs would have been
  caught the first time.

---

## 2026-08-24 · Phase 1 — Connection core + read path

- **PHASE-1:** `ConnectionManager` (`connections.py`): two asyncpg pools per DSN.
  readonly pool is eager (created at startup); every connection acquired from it runs
  `SET default_transaction_read_only = on`. This is the real enforcement mechanism —
  Postgres refuses writes at the executor level for a read-only transaction regardless
  of the DSN role's actual GRANTs, so a classifier bug can never get a write through the
  read path. readwrite pool is lazy (Phase 2 wires its first caller). Proven, not just
  asserted: `tests/test_connections.py` opens the readonly pool with the *superuser*
  test-container DSN and shows `INSERT` is still rejected.
- **PHASE-1:** `Classifier` (`classifier.py`, ADR-001): deny-by-default SQL
  classification — read / write / ddl / destructive / unknown. Allowlist shape, not
  blocklist: a statement must prove it's a pure read to be classified `read`. Built on
  `sqlparse` (pure-Python lexer) rather than `pglast`/libpg_query (rejected — C
  extension wheels per platform fight the <2min-install goal) or hand-rolled regex
  (rejected — can't reliably distinguish a string literal from a keyword, and CTEs need
  real tokenization). Catches writes hidden inside CTEs
  (`WITH x AS (INSERT ... RETURNING *) SELECT * FROM x`) by scanning every token for a
  `Keyword.DML` write, not just the leading keyword. Rejects multi-statement
  submissions outright (stacked-query injection shape). 26 table-driven cases in
  `tests/test_classifier.py`, including a string-literal false-positive check
  (`SELECT 'insert' AS label`).
- **PHASE-1:** `query.read` tool (`tools/query.py`): classifier gate (only `read`
  passes), row-limit enforcement via server-side cursor `.fetch(limit)` — no SQL
  rewriting/wrapping, since that breaks on EXPLAIN/CTEs/trailing semicolons and is one
  more place to string-manipulate untrusted SQL — and per-call `SET LOCAL
  statement_timeout` inside an explicit transaction, so a call's timeout tier can never
  leak onto the next caller reusing the same pooled connection.
- **PHASE-1:** `serialize.py`: one JSON-safety module for asyncpg row values (Decimal,
  datetime, UUID, bytea, ranges) shared by every tool that returns rows, instead of
  each tool inventing its own `str()`.
- **PHASE-1:** `schema.inspect` tool (`tools/schema.py`): summary/tables/full levels
  against `pg_catalog`/`information_schema` directly — no SQLAlchemy reflection
  dependency, no parsing `psql \d+` text output.
- **PHASE-1:** `db.health` tool (`tools/health.py`): connection counts by state, buffer
  cache hit ratio, dead-tuple bloat, long-running queries (>5s), waiting-lock chains —
  each finding carries a severity and a plain-language summary.
- **PHASE-1:** `__main__.py` wired: FastMCP server, three tools registered, `--dsn` /
  `--read-only` / `--selfcheck` CLI flags. `--selfcheck` connects, introspects, and
  prints a summary without starting the MCP transport — verified end-to-end against the
  live dev stack (1.2M-row `orders` table, correct counts and byte sizes).

### Gate evidence

```
uv run pytest -q         # 37 passed (classifier table-driven + testcontainers-backed
                          # connection/query tests against real Postgres 16)
uv run ruff check .      # All checks passed!
uv run mypy src          # Success: no issues found in 11 source files
uv run pgops-mcp --selfcheck   # against dev stack:
  readonly pool: OK
  tables in public schema: 3
    - customers: ~30000 rows, 6062080 bytes
    - orders: ~1200000 rows, 119717888 bytes
    - products: ~500 rows, 139264 bytes
```

Confirmed live through the actual FastMCP server object (not just the underlying
functions): `server.list_tools()` → `schema_inspect_tool`, `query_read_tool`,
`db_health_tool`; `server.call_tool('db_health_tool', {})` returned a real health
report against the dev database.

---

## 2026-08-24 · Phase 0 — Bootstrap complete

- **PHASE-0:** `dev/init.sql` seeded for real: ~30k customers, 500 products, ~1.2M
  orders via `generate_series`, with a skewed customer_id distribution (some customers
  own a disproportionate share of orders) so later phases have a real seq-scan /
  missing-index / lock-impact story instead of a toy table. Deliberately only one index
  exists (`orders.customer_id`) so `index.advise` (Phase 3) and lock-impact analysis
  (Phase 4) have something real to find.
- **PHASE-0:** `docker-compose.yml`: found port 5432 already bound by another local
  Postgres instance on this machine (a real "unfamiliar environment" collision, not a
  hypothetical) — remapped to host `5435` (later moved again when another project
  claimed `5433`), added a `pg_isready` healthcheck, named the
  container so it doesn't collide with the many other same-named `postgres` containers
  already running locally.
- **PHASE-0:** `pyproject.toml`: `uv_build` needs `[tool.uv.build-backend] module-name`
  when the import package name (`pgops`) doesn't match the normalized project name
  (`pgops_mcp`) — `uv sync` failed with "Expected a Python module at
  src\pgops_mcp\__init__.py" until this was set explicitly.
- **PHASE-0:** Verified live: `docker compose up -d` → healthy, seed counts confirmed
  via `psql` inside the container (30000 / 500 / 1200000); `uv sync --extra dev` →
  `uv run pytest -q` passes.

### Gate evidence

```
docker exec pgops_dev_postgres psql -U pgops -d pgops_demo -c \
  "SELECT (SELECT count(*) FROM customers) c, (SELECT count(*) FROM products) p, \
          (SELECT count(*) FROM orders) o;"
   c   |  p  |    o
-------+-----+---------
 30000 | 500 | 1200000

uv run pytest -q      # 1 passed
```

---

## 2026-08-24 · Phase 0 — Inception

- **PHASE-0:** Project selected after marketplace research (official MCP registry,
  Smithery ~17k servers, mcp.so). Verified gaps: existing Postgres MCPs are thin
  query/introspection wrappers; none do migrations with lock analysis, performance
  diagnosis, or Docker environment awareness. Alternatives evaluated and rejected:
  Tally MCP (no license access), WhatsApp Business (user rejected), durable-jobs MCP
  (surface reads as "just cron"), Zoho (user rejected).
- **PHASE-0:** Wrote full docs set: PRD (goals G1–G5, FRs, acceptance criteria),
  SPEC (7 phases with hard gates), ARCHITECTURE (diagrams + decision table),
  TOOLS (full catalog), ADR-001..005.
- **PHASE-0:** Decided stack: Python 3.12+, FastMCP, asyncpg, docker SDK, pytest +
  testcontainers, uv. stdio transport first (ADR-002).
- **PHASE-0:** No code yet by design — implementation starts at Phase 1 of SPEC.md.

### Gate evidence

(none yet)

---

## Next up

Everything the previous "Next up" listed has shipped — sampling, completions,
`migration.rollback`, the audit-log subject, the volatile-function classifier gap, and
packaging. What remains is listed honestly rather than quietly dropped.

### Blocking a first release

- [ ] **Publish.** The pipeline is written and its preconditions verified locally, but
      nothing has been pushed yet. Requires, in order: a PyPI project with Trusted
      Publishing configured for this repo, a `release` GitHub environment, then a `v*`
      tag. The MCP Registry entry is the last step and is automatic.
- [ ] **Verify on a machine that is not this one.** The clean-room test proves the wheel
      installs into an empty venv on *this* Windows host. Linux and macOS are covered by
      CI running the suite, not by anyone actually installing the published artifact.

### Known limits, deliberately not built

- [ ] **Per-session DSN isolation for HTTP.** Auth identifies the caller and scopes
      limit *what* they may do, but every authenticated caller still shares one
      `ConnectionManager`, one audit log and one in-memory plan/token store. Correct for
      the intended shape (one engineer, one or a few databases); insufficient for
      multi-tenant SaaS. Documented as a non-goal in SETUP.md rather than half-built.
- [ ] **`index.advise` names the table taking sequential scans, not the column** to
      index — that needs per-statement plan inspection. It currently says so ("run
      `query.explain` on this statement") instead of fabricating a `CREATE INDEX`.
- [ ] **`DROP INDEX` / `DROP CONSTRAINT` are irreversible in `migration.rollback`**,
      because the object's definition is not captured before the drop. Recoverable in
      principle: record `pg_get_indexdef` / `pg_get_constraintdef` at plan time. Until
      then the rollback refuses and says why, rather than reconstructing a `CREATE`
      statement from assumptions.

### Worth doing, not urgent

- [ ] **Smithery listing** — a second marketplace, separate manifest format from
      `server.json`.
- [ ] **Demo recording.** The lock-analysis output on a 1.2M-row table is the single
      most convincing artifact this project has and it currently only exists as text.
