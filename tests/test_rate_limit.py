"""Per-caller tool-call rate limiting, and server version reporting.

A tool executes whenever a model decides to call it. Statement timeouts, row caps and
pool sizes bound what any *single* call costs the database; none of them bound how many
calls arrive, and none distinguish one caller from another.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from conftest import free_port

from pgops.auth import Scope, build_verifier, generate_keypair, issue_token
from pgops.middleware import ToolCallRateLimit


class _Msg:
    name = "query.read"


class _Ctx:
    message = _Msg()


async def _call(limiter: ToolCallRateLimit) -> bool:
    """True if the call was allowed."""
    from fastmcp.exceptions import ToolError

    async def call_next(_ctx: Any) -> str:
        return "ok"

    try:
        await limiter.on_call_tool(_Ctx(), call_next)  # type: ignore[arg-type]
    except ToolError:
        return False
    return True


async def test_burst_is_allowed_then_the_limit_applies() -> None:
    limiter = ToolCallRateLimit(requests_per_second=1, burst=3)
    results = [await _call(limiter) for _ in range(6)]
    assert results[:3] == [True, True, True]
    assert results[3:] == [False, False, False]


async def test_budgets_are_per_caller_not_global(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reason a global limit is the wrong shape: it lets one noisy agent starve
    every other caller, which is precisely the outcome auth exists to prevent."""
    from pgops import middleware
    from pgops.middleware import Caller

    limiter = ToolCallRateLimit(requests_per_second=1, burst=2)
    current = {"subject": "loud-agent"}
    monkeypatch.setattr(
        middleware,
        "current_caller",
        lambda: Caller(subject=current["subject"], scopes=frozenset(), authenticated=True),
    )

    assert [await _call(limiter) for _ in range(4)] == [True, True, False, False]

    current["subject"] = "quiet-agent"
    assert await _call(limiter) is True, "one caller's spending must not exhaust another's"


async def test_refusal_tells_the_model_what_to_do() -> None:
    """The caller is a model. A message it can act on beats a code it has to infer."""
    from fastmcp.exceptions import ToolError

    limiter = ToolCallRateLimit(requests_per_second=1, burst=1)
    await _call(limiter)

    async def call_next(_ctx: Any) -> str:
        return "ok"

    with pytest.raises(ToolError) as exc_info:
        await limiter.on_call_tool(_Ctx(), call_next)  # type: ignore[arg-type]
    assert "retry" in str(exc_info.value).lower()


async def test_bucket_refills_over_time() -> None:
    limiter = ToolCallRateLimit(requests_per_second=50, burst=1)
    assert await _call(limiter) is True
    assert await _call(limiter) is False
    await asyncio.sleep(0.1)  # 50/s refills one token in 20ms
    assert await _call(limiter) is True


# --- live -----------------------------------------------------------------------------


@pytest.mark.slow
async def test_handshake_does_not_consume_the_tool_budget(conn_manager: Any, config: Any) -> None:
    """Regression test for the bug this middleware exists to avoid.

    FastMCP's built-in `RateLimitingMiddleware` hooks `on_message`, so it counts
    `initialize`, `notifications/initialized`, `tools/list` and every other protocol
    message. Measured with capacity 3, a client's opening handshake consumed the whole
    bucket before it issued a single tool call — 0 of 12 calls succeeded, and a
    legitimate session was rate-limited at connect time.

    Limiting `on_call_tool` instead means the budget is spent on the thing that is
    actually expensive.
    """
    from fastmcp import Client

    from pgops.__main__ import build_server

    config.rate_limit_rps = 2
    config.rate_limit_burst = 3

    pair = generate_keypair()
    server = build_server(config, conn_manager, auth=build_verifier(pair.public_key))
    port = free_port()
    task = asyncio.create_task(server.run_async(transport="http", host="127.0.0.1", port=port))
    await asyncio.sleep(4)

    try:
        token = issue_token(pair.private_key, subject="probe", scopes=[Scope.READ.value])
        async with Client(f"http://127.0.0.1:{port}/mcp/", auth=token) as client:
            # Listing is cheap and idempotent; it must not spend the budget either.
            await client.list_tools()
            await client.list_tools()

            allowed = 0
            for _ in range(6):
                try:
                    await client.call_tool("query.read", {"sql": "SELECT 1 AS n"})
                    allowed += 1
                except Exception as exc:  # noqa: BLE001 - refusal is expected
                    assert "rate limit" in str(exc).lower()
            assert allowed == 3, f"burst capacity was 3, got {allowed} through"
    finally:
        task.cancel()


@pytest.mark.slow
async def test_server_reports_its_version_to_clients(conn_manager: Any, config: Any) -> None:
    """Sent in the `initialize` handshake as serverInfo, which is how a client adapts to
    a changed tool signature. Without it the server reports no version at all."""
    from fastmcp import Client

    from pgops import __version__
    from pgops.__main__ import build_server

    async with Client(build_server(config, conn_manager)) as client:
        info = client.initialize_result.serverInfo
        assert info.name == "pgops-mcp"
        assert info.version == __version__
