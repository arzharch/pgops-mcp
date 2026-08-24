"""query.explain — run EXPLAIN and turn the plan into actionable verdicts.

The safety problem this tool has, which the read-path tools do not:

    EXPLAIN ANALYZE DELETE FROM orders

is not a description of a delete. It **performs** the delete — `ANALYZE` means "execute
the statement and report real timings". A tool that treats "explain" as inherently
read-only is a tool that will eventually delete a production table because an agent
wanted to know why a query was slow.

How that is handled here:

- `analyze=False` (the default) never executes anything: the planner produces a plan and
  the statement is not run. Safe for any statement, so it runs on the readonly pool.
- `analyze=True` on a **read** statement executes a read. Also the readonly pool.
- `analyze=True` on a **mutating** statement is the dangerous case. It runs inside an
  explicit transaction that is **always rolled back** — real timings, no persisted
  change — and it goes through the same guardrail and confirmation-token path as
  `query.write`, plus an audit record. Rollback is not a complete undo (sequence values
  consumed by nextval() do not roll back, and neither do side effects inside functions
  the statement calls), so the confirmation gate stays rather than being waived on the
  strength of "we roll it back anyway".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import asyncpg

from pgops.audit import AuditEntry, AuditLog
from pgops.classifier import classify
from pgops.config import PgopsConfig
from pgops.connections import ConnectionManager
from pgops.errors import ErrorCode, PgopsError
from pgops.guardrails import ConfirmationTokenStore, evaluate
from pgops.plan_analysis import Verdict, parse_plan
from pgops.plan_analysis import analyze as analyze_plan
from pgops.sampling import SUMMARY_SYSTEM_PROMPT, sample_text
from pgops.timing import Elapsed


@dataclass(slots=True)
class ExplainResult:
    plan: dict[str, Any]
    verdicts: list[Verdict]
    analyzed: bool
    meta: dict[str, Any]
    summary: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "analyzed": self.analyzed,
            "plan": self.plan,
            "verdicts": [v.to_dict() for v in self.verdicts],
            **self.meta,
        }
        if self.summary:
            payload["summary"] = self.summary
        return payload


def _explain_prefix(analyze_flag: bool) -> str:
    # BUFFERS only carries data when the statement actually ran.
    options = "ANALYZE, BUFFERS, FORMAT JSON" if analyze_flag else "FORMAT JSON"
    return f"EXPLAIN ({options}) "


async def query_explain(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
    sql: str,
    analyze: bool = False,
    confirm_token: str | None = None,
    timeout_ms: int | None = None,
    summarize: bool = False,
    ctx: Any = None,
) -> ExplainResult:
    classification = classify(sql)
    resolved_timeout = config.timeouts.resolve(timeout_ms)
    explain_sql = _explain_prefix(analyze) + sql

    mutating = not classification.is_read

    if mutating and analyze:
        # Executing for real (then rolling back) — same gate as query.write.
        verdict = evaluate(classification, sql)
        if not verdict.allowed:
            if confirm_token is None:
                token = tokens.issue(sql, verdict.reason)
                audit.record(
                    AuditEntry(
                        tool="query.explain",
                        sql=sql,
                        verdict="refused_pending_confirmation",
                        classification=classification.kind.value,
                        detail=f"EXPLAIN ANALYZE executes the statement: {verdict.reason}",
                    )
                )
                raise PgopsError(
                    ErrorCode.CONFIRMATION_REQUIRED,
                    f"EXPLAIN ANALYZE executes the statement. {verdict.reason}",
                    hint=(
                        f"the change is rolled back, but execution side effects (sequences, "
                        f"functions) are not — call again with confirm_token={token!r}, "
                        "or use analyze=false for a plan without executing"
                    ),
                )
            try:
                tokens.redeem(confirm_token, sql)
            except PgopsError as exc:
                audit.record(
                    AuditEntry(
                        tool="query.explain",
                        sql=sql,
                        verdict="refused_bad_token",
                        classification=classification.kind.value,
                        error_code=exc.code.value,
                        detail=exc.message,
                    )
                )
                raise

    elapsed = Elapsed()
    try:
        if mutating and analyze:
            raw = await _explain_mutating_rolled_back(conn_manager, explain_sql, resolved_timeout)
        else:
            raw = await _explain_readonly(conn_manager, explain_sql, resolved_timeout)
    except asyncpg.QueryCanceledError as exc:
        raise PgopsError(
            ErrorCode.QUERY_TIMEOUT,
            f"EXPLAIN exceeded {resolved_timeout}ms and was cancelled",
            hint="raise timeout_ms, or use analyze=false to plan without executing",
        ) from exc
    except asyncpg.PostgresError as exc:
        raise PgopsError(ErrorCode.INVALID_ARGUMENT, str(exc)) from exc

    duration_ms = elapsed.ms
    root, meta = parse_plan(raw)
    verdicts = analyze_plan(root)

    if mutating and analyze:
        audit.record(
            AuditEntry(
                tool="query.explain",
                sql=sql,
                verdict="executed_rolled_back",
                classification=classification.kind.value,
                duration_ms=round(duration_ms, 2),
                detail="EXPLAIN ANALYZE inside a rolled-back transaction",
            )
        )

    plan_dict = root.to_dict()
    summary = None
    if summarize:
        # Sampling asks the *client's* model, so this costs the user's tokens and is
        # opt-in rather than automatic. The plan and the deterministic verdicts are
        # returned either way — the summary is an addition to the evidence, never a
        # replacement for it, and a client without sampling support loses nothing else.
        summary = await sample_text(
            ctx,
            "Explain this PostgreSQL plan and where its time goes.\n\n"
            f"Statement:\n{sql}\n\n"
            # Truncated: a plan over a wide partitioned table can be enormous, and the
            # shape that explains the cost is at the top of the tree.
            f"Plan:\n{json.dumps(plan_dict, indent=2)[:12000]}\n\n"
            f"Analyzer verdicts:\n{json.dumps([v.to_dict() for v in verdicts], indent=2)}",
            SUMMARY_SYSTEM_PROMPT,
        )

    return ExplainResult(
        plan=plan_dict, verdicts=verdicts, analyzed=analyze, meta=meta, summary=summary
    )


async def _explain_readonly(
    conn_manager: ConnectionManager, explain_sql: str, timeout_ms: int
) -> Any:
    async with conn_manager.acquire_readonly() as conn, conn.transaction():
        await conn.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
        raw = await conn.fetchval(explain_sql)
    return json.loads(raw) if isinstance(raw, str) else raw


async def _explain_mutating_rolled_back(
    conn_manager: ConnectionManager, explain_sql: str, timeout_ms: int
) -> Any:
    """Run the statement for real, then discard the effect.

    The rollback is structural rather than conditional: the transaction context is
    exited via an exception that asyncpg's transaction manager treats as a failure, so
    there is no code path — including an unexpected error mid-parse — where the
    transaction commits. A `try/finally` calling rollback would be weaker; an early
    `return` inside the block would skip it.
    """

    class _Rollback(Exception):
        pass

    pool = await conn_manager.readwrite_pool()
    captured: Any = None
    try:
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
            raw = await conn.fetchval(explain_sql)
            captured = json.loads(raw) if isinstance(raw, str) else raw
            raise _Rollback
    except _Rollback:
        pass
    return captured
