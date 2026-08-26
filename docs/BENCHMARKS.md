# Benchmarks — what the numbers mean and what they're measured against

The live-server suite (`tests/test_live_server.py`, run with `uv run pytest tests/test_live_server.py -m live`)
measures real latency through the full deployed stack: MCP client → HTTP transport →
JWT verification → scope enforcement → observability middleware → tool boundary →
classifier → connection pool → Postgres. Every millisecond includes all of those hops.

## What the benchmarks are compared against

There is no external competitor benchmark — the comparison is against **explicit p95
latency budgets chosen as regression tripwires**, each with a stated failure mode it
catches. A budget is not a claim that "this is fast"; it is a line that, if crossed,
means something specific regressed.

| Scenario | Budget (p95) | Measured | What crossing it would mean |
|---|---|---|---|
| `query.read` (count over 250 rows) | 500 ms | ~34 ms | pool stopped being reused — per-call connection setup leaked into the hot path |
| `db.health` | 250 ms | ~34 ms | health queries became expensive; this is what a load balancer polls, so its p95 defines how fast a dead DB is detected |
| denied call (`query.write` w/ read token) | 200 ms | ~21 ms | denials should be *cheaper* than reads (no DB touch); parity means middleware grew a hidden pool dependency |
| 10 concurrent reads (batch wall time) | < 8× single-call p50 | ~320–450 ms vs ~65 ms single | reads serializing: pool exhaustion or an accidental global lock |

Budgets are deliberately generous (~10× headroom on current hardware) because their job
is catching regressions, not winning races. The concurrency threshold is 8× rather than
lower because the streamable-HTTP session multiplexes requests over one connection —
some transport-level serialization is expected; server-side serialization is not.

## Environment these numbers were measured in

- Windows dev machine, Docker Desktop, Postgres 16 container on host port 5435
- Server started fresh per test session via testcontainers-backed fixtures
- n=20 samples per scenario, percentiles computed exactly (not estimated)
- Numbers vary by machine; the budgets are what's portable, not the milliseconds

## Benchmark evidence as artifacts

Setting `PGOPS_BENCH_OUT=<path>` makes every benchmark append a JSONL record to that
file. CI does this on every run of the live suite and uploads the result as an
artifact (`benchmarks-<sha>`), so each commit carries a dated receipt of what its
budgets measured — a latency regression is visible in artifact history even after the
gate has already been fixed and passes again.

Two record shapes, matching what each scenario actually measures:

```jsonl
{"ts": "...", "scenario": "query.read(count)", "n": 20, "p50_ms": 14.27, "p95_ms": 15.42, "p99_ms": 15.42, "budget_p95_ms": 500}
{"ts": "...", "scenario": "concurrent(10 reads)", "batch_ms": 226.45, "single_p50_ms": 45.49, "ratio": 4.98, "budget_ratio": 8.0}
```

The concurrency scenario records one wall-clock batch number plus the ratio against
single-call p50 — it deliberately does *not* fabricate per-call samples.

## Also asserted (correctness evals, same suite)

- Full verdict taxonomy reachable end-to-end: `executed` / `refused` / `denied` / `failed`
- Safety guarantees survive the network hop: unbounded DELETE refused, token binding and
  single-use hold, rows untouched while gated
- Row limits are structured errors above the cap, honest `truncated` flags below it

## Historical note

The first run of this suite found a real authorization bug: a confirmation token issued
for a refused statement could be redeemed on any allowed statement. That finding is the
strongest argument for the suite: token binding was correct in the unit tests and
wrong end to end, because only a live server exercises the path where a token issued
by one tool is presented to another.
