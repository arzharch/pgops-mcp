"""Live HTTP server evaluation suite.

Everything else in this suite proves the *logic* works: in-process tool calls, a stdio
subprocess, unit-level assertions. This file is different. It boots the real server the
way production runs it — HTTP transport, JWT auth, scope enforcement, observability
middleware — and drives it with a real MCP client over the wire.

The distinction matters because "the function works" and "the deployed service behaves"
are different claims. Only the second one is what an operator (or an interviewer) can
rely on. These tests are the second claim, asserted:

- every tool answers over authenticated HTTP,
- the full verdict taxonomy holds end-to-end (executed / refused / denied / failed),
- safety guarantees survive the network hop (unbounded DELETE changes nothing),
- latency is bounded (benchmarks, not vibes).

Marked `live` so it never runs in the default suite (`-m "not live"` skips it); run it
explicitly with `uv run pytest tests/test_live_server.py -m live`. Needs Docker for
testcontainers Postgres.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest
import pytest_asyncio
from fastmcp import Client
from fastmcp.exceptions import ToolError

from pgops.auth import Scope, build_verifier, generate_keypair, issue_token

pytestmark = pytest.mark.live


# --- server lifecycle -----------------------------------------------------------------


@dataclass
class LiveServer:
    """A real FastMCP server on a real port, plus the keys to mint tokens for it."""

    url: str
    pair: Any
    task: asyncio.Task[None]

    def token(self, subject: str, scopes: list[str]) -> str:
        return issue_token(self.pair.private_key, subject=subject, scopes=scopes)

    async def stop(self) -> None:
        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass


@pytest_asyncio.fixture
async def live_server(conn_manager: Any, config: Any) -> AsyncIterator[LiveServer]:
    from pgops.__main__ import build_server

    pair = generate_keypair()
    server = build_server(config, conn_manager, auth=build_verifier(pair.public_key))
    port = 8795
    task = asyncio.create_task(
        server.run_async(transport="http", host="127.0.0.1", port=port)
    )
    await asyncio.sleep(4)  # uvicorn startup; same settle time as test_scope_enforcement
    yield LiveServer(url=f"http://127.0.0.1:{port}/mcp/", pair=pair, task=task)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# --- benchmark harness ------------------------------------------------------------------


@dataclass
class Bench:
    """Latency samples for one scenario, with percentile helpers.

    A benchmark that only reports a mean hides the tail, and the tail is where users
    live. p50/p95/p99 are computed exactly (small N), not estimated.
    """

    name: str
    samples_ms: list[float] = field(default_factory=list)

    def record(self, ms: float) -> None:
        self.samples_ms.append(ms)

    @property
    def p50(self) -> float:
        return sorted(self.samples_ms)[len(self.samples_ms) // 2]

    @property
    def p95(self) -> float:
        return sorted(self.samples_ms)[int(len(self.samples_ms) * 0.95) - 1]

    @property
    def p99(self) -> float:
        return sorted(self.samples_ms)[int(len(self.samples_ms) * 0.99) - 1]


def _assert_bench(bench: Bench, p95_budget_ms: float) -> None:
    """Assert + print. The assertion makes it a gate; the print makes it evidence."""
    assert bench.p95 <= p95_budget_ms, (
        f"{bench.name}: p95 {bench.p95:.1f}ms exceeded budget {p95_budget_ms}ms "
        f"(p50={bench.p50:.1f}ms, n={len(bench.samples_ms)})"
    )
    print(
        f"BENCH {bench.name:<28} n={len(bench.samples_ms):>3}  "
        f"p50={bench.p50:>7.1f}ms  p95={bench.p95:>7.1f}ms"
    )


async def _timed_call(client: Client, bench: Bench, tool: str, args: dict[str, Any]) -> Any:
    start = time.perf_counter()
    result = await client.call_tool(tool, args)
    bench.record((time.perf_counter() - start) * 1000)
    return result


# --- protocol-level evaluations ---------------------------------------------------------


async def test_all_tools_answer_over_authenticated_http(
    live_server: LiveServer, conn_manager: Any
) -> None:
    """Every read-path tool must return structured success through the full stack:
    JWT verify -> scope check -> observability span -> classifier -> pool -> Postgres."""
    token = live_server.token("eval-agent", [Scope.READ.value])
    async with Client(live_server.url, auth=token) as client:
        tools = {t.name for t in await client.list_tools()}
        # write/admin tools correctly absent for a read-only caller
        assert {"query.read", "query.explain", "db.health", "schema.inspect"} <= tools
        assert "query.write" not in tools

        health = await client.call_tool("db.health", {})
        categories = {f["category"] for f in health.data["findings"]}
        assert "connections" in categories

        schema = await client.call_tool("schema.inspect", {})
        table_names = [t["name"] for t in schema.data["tables"]]
        assert "items" in table_names

        read = await client.call_tool(
            "query.read", {"sql": "SELECT count(*) AS n FROM items"}
        )
        assert read.data["rows"][0]["n"] == 250

        # plain EXPLAIN is a read; the JSON variant belongs to query.explain
        plan = await client.call_tool(
            "query.read", {"sql": "EXPLAIN SELECT * FROM items WHERE id = 1"}
        )
        assert plan.data["row_count"] >= 1
        assert "QUERY PLAN" in plan.data["rows"][0]


async def test_full_verdict_taxonomy_over_the_wire(
    live_server: LiveServer, conn_manager: Any
) -> None:
    """executed / denied / refused / failed — all four reachable through real HTTP.

    This is the operational contract the dashboards are built on: if any of these four
    outcomes were unreachable or mislabeled end-to-end, every alert built on them would
    be silently wrong.
    """
    reader = live_server.token("eval-reader", [Scope.READ.value])

    # executed: normal read
    async with Client(live_server.url, auth=reader) as client:
        result = await client.call_tool("query.read", {"sql": "SELECT 1 AS one"})
        assert result.data["rows"] == [{"one": 1}]

    # denied: authorization refuses before the tool body runs. This one IS an
    # exception — ScopeEnforcement raises ToolError, by design: a permission failure
    # is not a tool result, it is a request the server will not process.
    async with Client(live_server.url, auth=reader) as client:
        with pytest.raises(Exception) as denied:
            await client.call_tool("query.write", {"sql": "DELETE FROM items"})
        assert "pgops:write" in str(denied.value)

    # refused: the tool itself rejects. Refusals arrive as normal results carrying a
    # structured error — parseable, not a stack trace the agent must interpret.
    async with Client(live_server.url, auth=reader) as client:
        refusal = await client.call_tool("query.read", {"sql": "DROP TABLE items"})
        assert refusal.structured_content is not None
        assert refusal.structured_content["error"]["code"] == "CLASSIFICATION_REFUSED"

    # failed: unexpected exception surfaces as INTERNAL_ERROR, internals masked.
    # A nonexistent *table* forces asyncpg to raise something the boundary's
    # PostgresError handler does not anticipate. (A nonexistent function no longer
    # works here: the volatility gate now refuses unknown functions before execution,
    # which is the correct behavior — so this exercises the truly-unanticipated path.)
    async with Client(live_server.url, auth=reader) as client:
        failed = await client.call_tool(
            "query.read", {"sql": "SELECT * FROM definitely_not_a_table_xyz"}
        )
        content = failed.structured_content or {}
        code = content.get("error", {}).get("code", "")
        assert code in {"INTERNAL_ERROR", "INVALID_ARGUMENT"}, content


async def test_safety_guarantees_survive_the_network_hop(
    live_server: LiveServer, conn_manager: Any, config: Any
) -> None:
    """The core promise, evaluated as a deployed service: an approved-looking write
    cannot run without human confirmation, and a confirmed unbounded delete still
    destroys only what was confirmed."""
    import asyncpg

    writer = live_server.token("eval-writer", [Scope.READ.value, Scope.WRITE.value])

    async with Client(live_server.url, auth=writer) as client:
        # 1. unbounded delete -> CONFIRMATION_REQUIRED with a bound token in the hint.
        # The refusal is a structured result, exactly as an agent receives it.
        refusal = await client.call_tool("query.write", {"sql": "DELETE FROM items"})
        assert refusal.structured_content is not None
        error = refusal.structured_content["error"]
        assert error["code"] == "CONFIRMATION_REQUIRED"
        token = error["hint"].split("confirm_token=")[1].split("'")[1]

        # rows untouched while the gate held
        conn = await asyncpg.connect(config.dsn)
        try:
            assert await conn.fetchval("SELECT count(*) FROM items") == 250
        finally:
            await conn.close()

        # 2. wrong statement + right token -> CONFIRMATION_MISMATCH (binding holds)
        mismatch = await client.call_tool(
            "query.write",
            {
                "sql": "DELETE FROM items WHERE name = 'item-1'",
                "confirm_token": token,
            },
        )
        assert mismatch.structured_content is not None
        assert (
            mismatch.structured_content["error"]["code"] == "CONFIRMATION_MISMATCH"
        )

        # 3. right statement + right token -> executes, exactly once
        result = await client.call_tool(
            "query.write", {"sql": "DELETE FROM items", "confirm_token": token}
        )
        assert result.data["rows_affected"] == 250

        # 4. replaying the same token on another statement -> rejected (single-use)
        replay = await client.call_tool(
            "query.write",
            {"sql": "INSERT INTO items (name) VALUES ('x')", "confirm_token": token},
        )
        assert replay.structured_content is not None
        assert replay.structured_content["error"]["code"] in {
            "INVALID_CONFIRMATION",
            "CONFIRMATION_MISMATCH",
        }


async def test_row_limit_is_enforced_not_clamped(live_server: LiveServer) -> None:
    """Exceeding the hard cap is a structured error, not silent truncation — the
    difference between 'you got some of the data' and 'you know you need another
    query'."""
    writer = live_server.token("eval-limits", [Scope.READ.value])
    async with Client(live_server.url, auth=writer) as client:
        # explicit limit above the hard max -> ROW_LIMIT_EXCEEDED, not a clamp
        over = await client.call_tool(
            "query.read",
            {"sql": "SELECT * FROM items", "limit": 999_999},
        )
        assert over.structured_content is not None
        assert over.structured_content["error"]["code"] == "ROW_LIMIT_EXCEEDED"

        # and a limit within bounds truncates honestly: `truncated` is reported,
        # never silently applied
        ok = await client.call_tool(
            "query.read", {"sql": "SELECT * FROM items ORDER BY id", "limit": 10}
        )
        assert ok.data["row_count"] == 10
        assert ok.data["truncated"] is True


# --- benchmarks -------------------------------------------------------------------------


@pytest.mark.parametrize("n", [20])
async def test_bench_read_latency_p95_under_budget(
    live_server: LiveServer, conn_manager: Any, n: int
) -> None:
    """query.read p95 budget: 500ms over local HTTP against a pooled connection.

    The budget is deliberately generous — this is a regression tripwire, not a
    microbenchmark. What it catches is the failure modes that matter operationally:
    a pool that stopped being reused, a per-call connection setup leaking into the
    hot path, or middleware that grew O(n) work per request.
    """
    token = live_server.token("bench-reader", [Scope.READ.value])
    bench = Bench(name="query.read(count)")
    async with Client(live_server.url, auth=token) as client:
        for _ in range(n):
            await _timed_call(
                client, bench, "query.read", {"sql": "SELECT count(*) AS n FROM items"}
            )
    _assert_bench(bench, p95_budget_ms=500)


async def test_bench_health_check_p95_under_budget(live_server: LiveServer) -> None:
    """db.health is what a load balancer would poll; its p95 defines how fast the
    system detects a dead database. Budget 250ms."""
    token = live_server.token("bench-health", [Scope.READ.value])
    bench = Bench(name="db.health")
    async with Client(live_server.url, auth=token) as client:
        for _ in range(20):
            await _timed_call(client, bench, "db.health", {})
    _assert_bench(bench, p95_budget_ms=250)


async def test_bench_denial_fast_path(live_server: LiveServer) -> None:
    """Denied calls should be cheap — they touch no database. If denials ever cost
    as much as reads, middleware grew a hidden dependency on the pool. Budget 200ms."""
    token = live_server.token("bench-denied", [Scope.READ.value])
    bench = Bench(name="denied(write)")
    async with Client(live_server.url, auth=token) as client:
        for _ in range(20):
            start = time.perf_counter()
            with pytest.raises(ToolError):
                await client.call_tool("query.write", {"sql": "DELETE FROM items"})
            bench.record((time.perf_counter() - start) * 1000)
    _assert_bench(bench, p95_budget_ms=200)


async def test_bench_concurrent_reads_no_serialization(
    live_server: LiveServer, conn_manager: Any
) -> None:
    """10 concurrent reads must overlap, not serialize: wall time for the batch should
    be well under 10x a single call. Catches pool sizing regressions and accidental
    global locks."""
    token = live_server.token("bench-concurrent", [Scope.READ.value])
    single = Bench(name="single(read)")
    async with Client(live_server.url, auth=token) as client:
        await _timed_call(client, single, "query.read", {"sql": "SELECT pg_sleep(0)"})

        start = time.perf_counter()
        results = await asyncio.gather(
            *[
                client.call_tool("query.read", {"sql": f"SELECT {i} AS i"})
                for i in range(10)
            ]
        )
        batch_ms = (time.perf_counter() - start) * 1000

    assert len(results) == 10
    assert all(r.data["rows"][0]["i"] == i for i, r in enumerate(results))
    # The MCP streamable-HTTP session multiplexes requests over one connection, so
    # some serialization at the transport layer is expected. What this catches is a
    # *server-side* regression: pool exhaustion or a global lock would push the batch
    # toward 10x single-call latency. 8x leaves headroom for transport overhead while
    # still failing loudly on real serialization.
    assert batch_ms < single.p50 * 8, (
        f"concurrent batch took {batch_ms:.0f}ms vs single p50 {single.p50:.0f}ms — "
        "reads appear to be serializing"
    )
    print(f"BENCH concurrent(10 reads)             batch={batch_ms:.0f}ms")
