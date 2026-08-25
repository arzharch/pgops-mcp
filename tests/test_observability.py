"""Observability: spans, metrics, health endpoints.

The design constraint that shapes everything here: **telemetry must never break the
operation it describes.** With PGOPS_OTEL_ENDPOINT unset, every call in this module is
a cheap no-op — the tests assert that explicitly, because a monitoring layer that can
fail the request it monitors is worse than no monitoring at all.
"""

from __future__ import annotations

from pgops.errors import ErrorCode, PgopsError, tool_boundary


async def test_noop_without_configuration() -> None:
    """With no OTel endpoint configured, tools run normally and ToolSpan is inert."""
    from pgops.observability import ToolSpan

    @tool_boundary
    async def ok_tool() -> dict[str, object]:
        with ToolSpan("ok_tool") as span:
            span.set_verdict("executed")
        return {"ok": True}

    result = await ok_tool()
    assert result == {"ok": True}


async def test_refusal_still_returns_structured_error() -> None:
    """Instrumentation wraps the boundary; refusals behave identically."""
    from pgops.observability import ToolSpan

    @tool_boundary
    async def refusing_tool() -> dict[str, object]:
        with ToolSpan("refusing_tool"):
            raise PgopsError(ErrorCode.INVALID_ARGUMENT, "nope")

    result = await refusing_tool()
    assert result["error"]["code"] == "INVALID_ARGUMENT"


async def test_unexpected_exception_still_masked() -> None:
    from pgops.observability import ToolSpan

    @tool_boundary
    async def broken_tool() -> dict[str, object]:
        with ToolSpan("broken_tool"):
            raise ValueError("secret internal detail")

    result = await broken_tool()
    assert result["error"]["code"] == "INTERNAL_ERROR"
    assert "secret" not in str(result)


def test_config_defaults_to_disabled() -> None:
    import os

    from pgops.observability import health_port, metrics_port, otel_endpoint

    # These read env vars; in the test environment they are unset.
    for name in ("PGOPS_OTEL_ENDPOINT", "PGOPS_METRICS_PORT", "PGOPS_HEALTH_PORT"):
        os.environ.pop(name, None)
    assert otel_endpoint() is None
    assert metrics_port() is None
    assert health_port() is None


def test_init_is_safe_without_endpoint() -> None:
    from pgops.observability import init_observability

    assert init_observability() is False


def test_span_helpers_are_inert_when_disabled() -> None:
    """record_pool_timeout / set_db_up must not raise without providers."""
    from pgops.observability import record_pool_timeout, set_db_up

    record_pool_timeout()
    set_db_up(True)
    set_db_up(False)


async def test_health_endpoints_skip_without_port() -> None:
    """run_health_endpoints returns immediately when PGOPS_HEALTH_PORT is unset."""
    import os

    from pgops.observability import run_health_endpoints

    os.environ.pop("PGOPS_HEALTH_PORT", None)

    async def check() -> bool:
        return True

    await run_health_endpoints(check)  # returns, does not serve
