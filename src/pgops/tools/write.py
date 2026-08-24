"""query.write — classification → guardrail evaluation → (token check) → execution.

The ordering matters and is deliberate:

    classify → guardrails → token → execute → audit

Classification happens before anything else so the guardrails reason about a *class*,
not about SQL text. The token check happens after guardrail evaluation so that a token
can only ever unblock a specific, already-identified risk — it is not a general
"skip safety" flag. Execution is last, and the audit record is written on every path,
including refusals, because a refused destructive statement is precisely the event an
incident review needs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import asyncpg

from pgops.audit import AuditEntry, AuditLog
from pgops.classifier import classify
from pgops.config import PgopsConfig
from pgops.connections import ConnectionManager
from pgops.errors import ErrorCode, PgopsError
from pgops.guardrails import ConfirmationTokenStore, evaluate


@dataclass(slots=True)
class QueryWriteResult:
    rows_affected: int
    duration_ms: float
    audit_id: str
    classification: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows_affected": self.rows_affected,
            "duration_ms": round(self.duration_ms, 2),
            "audit_id": self.audit_id,
            "classification": self.classification,
        }


def _rows_affected(status: str) -> int:
    """asyncpg returns the raw Postgres command tag ('DELETE 42', 'INSERT 0 7')."""
    parts = status.split()
    return int(parts[-1]) if parts and parts[-1].isdigit() else 0


async def query_write(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
    sql: str,
    confirm_token: str | None = None,
    timeout_ms: int | None = None,
) -> QueryWriteResult:
    classification = classify(sql)

    if classification.is_read:
        raise PgopsError(
            ErrorCode.INVALID_ARGUMENT,
            "statement is a pure read; use query.read",
            hint="query.write is for INSERT/UPDATE/DELETE/DDL",
        )

    verdict = evaluate(classification, sql)

    if not verdict.allowed:
        if confirm_token is None:
            # Refusal path: issue a token bound to this exact statement, and record the
            # refusal. The agent must relay `reason` to a human to get approval.
            token = tokens.issue(sql, verdict.reason)
            audit.record(
                AuditEntry(
                    tool="query.write",
                    sql=sql,
                    verdict="refused_pending_confirmation",
                    classification=classification.kind.value,
                    detail=verdict.reason,
                )
            )
            raise PgopsError(
                ErrorCode.CONFIRMATION_REQUIRED,
                verdict.reason,
                hint=(
                    f"if this is intended, call query.write again with "
                    f"confirm_token={token!r} (single use, expires in 5 minutes)"
                ),
            )
        # A token was supplied — it must match this statement, be unexpired and unused.
        # redeem() raises on every failure mode; nothing falls through to execution.
        try:
            tokens.redeem(confirm_token, sql)
        except PgopsError as exc:
            audit.record(
                AuditEntry(
                    tool="query.write",
                    sql=sql,
                    verdict="refused_bad_token",
                    classification=classification.kind.value,
                    error_code=exc.code.value,
                    detail=exc.message,
                )
            )
            raise

    resolved_timeout = config.timeouts.resolve(timeout_ms)
    pool = await conn_manager.readwrite_pool()

    start = time.monotonic()
    try:
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(f"SET LOCAL statement_timeout = {int(resolved_timeout)}")
            status = await conn.execute(sql)
    except asyncpg.QueryCanceledError as exc:
        audit.record(
            AuditEntry(
                tool="query.write",
                sql=sql,
                verdict="failed",
                classification=classification.kind.value,
                error_code=ErrorCode.QUERY_TIMEOUT.value,
                detail=f"exceeded {resolved_timeout}ms",
            )
        )
        raise PgopsError(
            ErrorCode.QUERY_TIMEOUT,
            f"statement exceeded {resolved_timeout}ms and was cancelled and rolled back",
        ) from exc
    except asyncpg.PostgresError as exc:
        audit.record(
            AuditEntry(
                tool="query.write",
                sql=sql,
                verdict="failed",
                classification=classification.kind.value,
                error_code=ErrorCode.INVALID_ARGUMENT.value,
                detail=str(exc),
            )
        )
        raise PgopsError(ErrorCode.INVALID_ARGUMENT, str(exc)) from exc

    duration_ms = (time.monotonic() - start) * 1000
    rows = _rows_affected(status)
    audit_id = audit.record(
        AuditEntry(
            tool="query.write",
            sql=sql,
            verdict="executed",
            classification=classification.kind.value,
            duration_ms=round(duration_ms, 2),
            rows_affected=rows,
        )
    )
    return QueryWriteResult(
        rows_affected=rows,
        duration_ms=duration_ms,
        audit_id=audit_id,
        classification=classification.kind.value,
    )
