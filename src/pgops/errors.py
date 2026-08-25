"""Structured error codes. No tool ever leaks a raw exception/traceback to a client —
every failure is converted to a PgopsError and rendered as {"error": {...}} (TOOLS.md)."""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

from pgops.observability import ToolSpan

logger = logging.getLogger("pgops")


class ErrorCode(StrEnum):
    DSN_MISSING = "DSN_MISSING"
    CONNECTION_FAILED = "CONNECTION_FAILED"
    CLASSIFICATION_REFUSED = "CLASSIFICATION_REFUSED"
    MULTI_STATEMENT_REJECTED = "MULTI_STATEMENT_REJECTED"
    ROW_LIMIT_EXCEEDED = "ROW_LIMIT_EXCEEDED"
    QUERY_TIMEOUT = "QUERY_TIMEOUT"
    POOL_EXHAUSTED = "POOL_EXHAUSTED"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    INVALID_CONFIRMATION = "INVALID_CONFIRMATION"
    CONFIRMATION_MISMATCH = "CONFIRMATION_MISMATCH"
    CONFIRMATION_DECLINED = "CONFIRMATION_DECLINED"
    MIGRATION_IN_FLIGHT = "MIGRATION_IN_FLIGHT"
    MIGRATION_FAILED = "MIGRATION_FAILED"
    MIGRATION_IRREVERSIBLE = "MIGRATION_IRREVERSIBLE"
    DOCKER_UNAVAILABLE = "DOCKER_UNAVAILABLE"
    CONTAINER_NOT_FOUND = "CONTAINER_NOT_FOUND"
    APPROVAL_MODE_REQUIRED = "APPROVAL_MODE_REQUIRED"
    EXEC_NOT_ALLOWED = "EXEC_NOT_ALLOWED"
    SAMPLING_UNAVAILABLE = "SAMPLING_UNAVAILABLE"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    READ_ONLY_MODE = "READ_ONLY_MODE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class PgopsError(Exception):
    """Every tool-facing failure. Carries a machine-readable code plus a hint an
    agent can act on, so the caller never has to regex a stack trace."""

    def __init__(self, code: ErrorCode, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code.value, "message": self.message}
        if self.hint:
            body["hint"] = self.hint
        return {"error": body}


def tool_boundary[**P](
    func: Callable[P, Awaitable[dict[str, Any]]],
) -> Callable[P, Awaitable[dict[str, Any]]]:
    """The single error boundary every MCP tool is wrapped in.

    SPEC cross-cutting rule #2 is "no raw error leakage", and a `except PgopsError`
    in each tool does NOT achieve that: it only catches the failures we already
    anticipated. Anything unforeseen — an asyncpg error from a code path that forgot to
    wrap it, a bug in our own parsing, a pool that went away — propagates out of the
    tool as a live exception, and the MCP framework renders it (traceback included) to
    whatever is driving the server. That's how internal detail reaches an agent, and
    then a user.

    So the boundary is inverted: catch PgopsError for the expected/actionable failures,
    then catch `Exception` for everything else and return a generic INTERNAL_ERROR.
    The full traceback goes to the log (stderr) where an operator can read it; the
    client gets a stable error code and nothing about our internals.
    """

    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> dict[str, Any]:
        # One span per tool call, created here so every tool is instrumented by
        # construction rather than by remembering to decorate each one. Refusals are
        # spans too: a spike in CONFIRMATION_REQUIRED is an operational signal.
        with ToolSpan(func.__name__) as span:
            try:
                result = await func(*args, **kwargs)
            except PgopsError as exc:
                logger.info("tool %s refused: %s %s", func.__name__, exc.code.value, exc.message)
                span.set_verdict("refused", error_code=exc.code.value)
                return exc.to_dict()
            except Exception as exc:
                # exc_info: operator sees the whole traceback on stderr; the caller does not.
                logger.exception("unhandled error in tool %s", func.__name__)
                span.record_exception(exc)
                span.set_verdict("failed", error_code=ErrorCode.INTERNAL_ERROR.value)
                return PgopsError(
                    ErrorCode.INTERNAL_ERROR,
                    "internal error; see server logs",
                ).to_dict()
            # Successes carry what happened for latency/traffic dashboards. The error
            # contract means refusals arrive as normal dicts with an "error" key —
            # those are counted as refusals, not successes.
            if isinstance(result, dict) and "error" in result:
                span.set_verdict(
                    "refused",
                    error_code=result.get("error", {}).get("code", "unknown"),
                )
            else:
                span.set_verdict("executed")
            return result

    return wrapper
