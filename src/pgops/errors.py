"""Structured error codes. No tool ever leaks a raw exception/traceback to a client —
every failure is converted to a PgopsError and rendered as {"error": {...}} (TOOLS.md)."""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

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
        try:
            return await func(*args, **kwargs)
        except PgopsError as exc:
            logger.info("tool %s refused: %s %s", func.__name__, exc.code.value, exc.message)
            return exc.to_dict()
        except Exception:
            # exc_info: operator sees the whole traceback on stderr; the caller does not.
            logger.exception("unhandled error in tool %s", func.__name__)
            return PgopsError(
                ErrorCode.INTERNAL_ERROR,
                "internal error; see server logs",
            ).to_dict()

    return wrapper
