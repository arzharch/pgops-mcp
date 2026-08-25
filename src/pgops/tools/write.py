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

from dataclasses import dataclass, replace
from typing import Any

import asyncpg

from pgops.approval import ApprovalMethod, request_approval
from pgops.audit import AuditEntry, AuditLog
from pgops.classifier import classify
from pgops.config import PgopsConfig
from pgops.connections import ConnectionManager
from pgops.errors import ErrorCode, PgopsError
from pgops.guardrails import ConfirmationTokenStore, evaluate
from pgops.timing import Elapsed


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
    ctx: Any = None,
) -> QueryWriteResult:
    classification = classify(sql)

    if classification.is_read:
        raise PgopsError(
            ErrorCode.INVALID_ARGUMENT,
            "statement is a pure read; use query.read",
            hint="query.write is for INSERT/UPDATE/DELETE/DDL",
        )

    verdict = evaluate(classification, sql)
    approval_method = "none"

    # A supplied token is ALWAYS redeemed against this exact statement, before any
    # other logic. The check cannot live inside the `not verdict.allowed` branch: a
    # token issued for a refused statement (e.g. an unbounded DELETE) must not be
    # spendable on a *different* statement that guardrails happen to allow — approval
    # for one statement is approval for that statement and nothing else. Redeeming
    # unconditionally also consumes the token, so a failed attempt can't be retried
    # with the same credential.
    if confirm_token is not None:
        try:
            tokens.redeem(confirm_token, sql)
            # Only a successful redeem grants approval; it never makes a refused
            # statement executable on its own — the verdict below still gates that.
            approval_method = ApprovalMethod.TOKEN.value
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

    # Preferred path: ask the human directly via elicitation. The token protocol routes
    # approval *through the model*, which can relay the reason inaccurately or simply
    # call again with the token it was just handed. Elicitation puts the question to the
    # user outside the model's turn, so the model cannot fabricate the answer. If the
    # client does not support it, fall back to tokens — degraded, but never to
    # "no approval needed".
    if not verdict.allowed and confirm_token is None:
        approval = await request_approval(ctx, f"Execute: {sql}", verdict.reason)
        if approval.approved:
            approval_method = approval.method.value
            audit.record(
                AuditEntry(
                    tool="query.write",
                    sql=sql,
                    verdict="approved_by_user",
                    classification=classification.kind.value,
                    detail=f"{verdict.reason} | approved via {approval.method.value}",
                )
            )
            verdict = replace(verdict, allowed=True)
        elif approval.method is ApprovalMethod.ELICITATION:
            # The human was asked and said no. An explicit refusal must not be
            # convertible into a token the agent can redeem a moment later.
            audit.record(
                AuditEntry(
                    tool="query.write",
                    sql=sql,
                    verdict="declined_by_user",
                    classification=classification.kind.value,
                    detail=approval.detail,
                )
            )
            raise PgopsError(
                ErrorCode.CONFIRMATION_DECLINED,
                f"the user declined this action: {verdict.reason}",
            )

    if not verdict.allowed and confirm_token is None:
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
    # A refused statement with a supplied token never reaches execution: the
    # unconditional redeem above validated the credential, but a valid token for a
    # different statement does not override the guardrail refusal — and if the token
    # matched this statement, redeem() consumed it before the guardrail check could
    # pass anyway (refused statements are never issued tokens for themselves here).

    resolved_timeout = config.timeouts.resolve(timeout_ms)
    pool = await conn_manager.readwrite_pool()

    elapsed = Elapsed()
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

    duration_ms = elapsed.ms
    rows = _rows_affected(status)
    audit_id = audit.record(
        AuditEntry(
            tool="query.write",
            sql=sql,
            verdict="executed",
            classification=classification.kind.value,
            duration_ms=round(duration_ms, 2),
            rows_affected=rows,
            # Record *how* this was approved. "The human was asked directly" and "the
            # agent presented a token" are different assurances, and an incident review
            # reconstructing a bad day needs to tell them apart.
            detail=f"approval={approval_method}" if approval_method != "none" else None,
        )
    )
    return QueryWriteResult(
        rows_affected=rows,
        duration_ms=duration_ms,
        audit_id=audit_id,
        classification=classification.kind.value,
    )
