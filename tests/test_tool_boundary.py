"""The error boundary wrapping every MCP tool (SPEC cross-cutting rule #2).

These are unit tests — no database — because the point is what happens to exceptions
regardless of where they came from.
"""

from __future__ import annotations

import logging
from typing import Any

from pgops.errors import ErrorCode, PgopsError, tool_boundary


async def test_success_passes_through() -> None:
    @tool_boundary
    async def tool() -> dict[str, Any]:
        return {"ok": True}

    assert await tool() == {"ok": True}


async def test_pgops_error_becomes_structured_response() -> None:
    @tool_boundary
    async def tool() -> dict[str, Any]:
        raise PgopsError(ErrorCode.INVALID_ARGUMENT, "bad input", hint="try harder")

    result = await tool()
    assert result == {
        "error": {"code": "INVALID_ARGUMENT", "message": "bad input", "hint": "try harder"}
    }


async def test_unexpected_exception_does_not_leak_internals() -> None:
    @tool_boundary
    async def tool() -> dict[str, Any]:
        raise ValueError("connection string postgres://user:hunter2@host/db failed")

    result = await tool()
    assert result["error"]["code"] == ErrorCode.INTERNAL_ERROR.value
    # the point of the boundary: the caller learns nothing about the failure's contents
    assert "hunter2" not in str(result)
    assert "ValueError" not in str(result)


async def test_unexpected_exception_is_logged_with_traceback(
    caplog: Any,
) -> None:
    @tool_boundary
    async def tool() -> dict[str, Any]:
        raise ValueError("boom")

    with caplog.at_level(logging.ERROR, logger="pgops"):
        await tool()
    # operator-facing side: full detail lands in the log even though the client saw none
    assert "unhandled error in tool" in caplog.text
    assert "ValueError: boom" in caplog.text


async def test_boundary_preserves_function_metadata() -> None:
    @tool_boundary
    async def my_tool_name() -> dict[str, Any]:
        """Docstring becomes the MCP tool description."""
        return {}

    # FastMCP reads __name__/__doc__ off the callable to build the tool schema, so the
    # decorator must not replace them with the wrapper's.
    assert my_tool_name.__name__ == "my_tool_name"
    assert my_tool_name.__doc__ is not None
    assert "MCP tool description" in my_tool_name.__doc__
