"""Structured error codes. No tool ever leaks a raw exception/traceback to a client —
every failure is converted to a PgopsError and rendered as {"error": {...}} (TOOLS.md)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    DSN_MISSING = "DSN_MISSING"
    CONNECTION_FAILED = "CONNECTION_FAILED"
    CLASSIFICATION_REFUSED = "CLASSIFICATION_REFUSED"
    MULTI_STATEMENT_REJECTED = "MULTI_STATEMENT_REJECTED"
    ROW_LIMIT_EXCEEDED = "ROW_LIMIT_EXCEEDED"
    QUERY_TIMEOUT = "QUERY_TIMEOUT"
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
