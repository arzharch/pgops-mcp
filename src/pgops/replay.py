"""Audit replay — turn the forensic log into a demonstrable capability.

The audit log already records everything needed to reconstruct what happened: tool,
SQL, verdict, actor, timestamp. Until now it answered "what happened" only by a human
reading JSONL. Replay makes it executable:

    pgops-mcp replay ~/.pgops/audit.jsonl --dry-run

Two modes:

- **--dry-run** (default): re-classifies every executed statement against the current
  classifier/guardrails and reports whether today's code would reach the same verdict.
  This is a *regression detector for the safety core*: if an old session's writes would
  now be classified differently, either the statements were ambiguous or the rules
  changed — both are worth knowing, and neither is visible from reading the log.

- **--execute**: actually re-runs the executed statements in order against the target
  database. This is for rebuilding state (a dev database restored from backup can be
  fast-forwarded through the audit trail). Deliberately gated behind a flag AND a
  typed confirmation, because replaying writes is itself a destructive operation.

Design decisions:

- Only `verdict == "executed"` entries replay. Refusals are history, not instructions:
  re-running them would defeat the guardrail that refused them the first time.
- Entries produced by a previous replay (`tool == "replay"`) are likewise skipped:
  replaying a live audit file must not compound its own output into the timeline.
- Statements replay in file order with their original actor recorded in the new audit
  entries (`actor` prefixed `replay:`), so a replayed session is distinguishable from
  live traffic in any later incident review.
- A failed statement stops the replay and reports exactly where — partial replay is
  safer than skipping errors silently, because skipped writes produce a database state
  that matches no point in the original timeline.
- Idempotence is NOT assumed: the same caveat as migration rollback applies. Replaying
  an INSERT twice duplicates rows; that's inherent to replaying a log of arbitrary SQL
  and is why --execute demands explicit confirmation.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pgops.audit import AuditEntry, AuditLog
from pgops.classifier import classify
from pgops.connections import ConnectionManager
from pgops.errors import PgopsError


@dataclass(slots=True)
class ReplayRow:
    """One audited entry's replay analysis."""

    ts: str
    actor: str
    sql: str
    original_verdict: str
    status: str  # "replayed" | "would-execute" | "divergent" | "skipped" | "failed"
    detail: str = ""


@dataclass(slots=True)
class ReplayReport:
    total_entries: int = 0
    executed_entries: int = 0
    replayed: int = 0
    divergent: int = 0
    skipped: int = 0
    failed_at: str | None = None
    rows: list[ReplayRow] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"audit entries: {self.total_entries}",
            f"executed statements: {self.executed_entries}",
        ]
        if self.failed_at:
            lines.append(f"FAILED AT: {self.failed_at}")
        else:
            lines.append(
                f"replayed: {self.replayed} · divergent classifications: {self.divergent}"
                f" · skipped (non-executed): {self.skipped}"
            )
        return "\n".join(lines)


def _classify_now(sql: str) -> tuple[str, bool]:
    """(classification kind, would-guardrails-allow) under *current* code."""
    classification = classify(sql)
    from pgops.guardrails import evaluate

    verdict = evaluate(classification, sql)
    return classification.kind.value, verdict.allowed


async def replay_audit_log(
    conn_manager: ConnectionManager,
    audit_path: Path,
    *,
    execute: bool,
    actor_filter: str | None = None,
    limit: int | None = None,
) -> ReplayReport:
    """Replay (or dry-run) executed statements from an audit log.

    See module docstring for mode semantics. Returns a report; never raises for
    per-statement failures (those are recorded and stop the run).
    """
    log = AuditLog(audit_path)
    entries = log.read_all()

    # An append-only JSONL file is already chronological (oldest first): every record()
    # appends to the end, so file order IS execution order. No reversal.
    if actor_filter:
        entries = [e for e in entries if e.get("actor") == actor_filter]
    if limit:
        entries = entries[-limit:]

    report = ReplayReport(total_entries=len(entries))
    replay_audit = AuditLog(audit_path)

    for entry in entries:
        verdict = entry.get("verdict", "")
        sql = entry.get("sql", "")
        row = ReplayRow(
            ts=entry.get("ts", ""),
            actor=entry.get("actor", ""),
            sql=sql,
            original_verdict=verdict,
            status="skipped",
        )

        if verdict != "executed" or entry.get("tool") == "replay":
            report.skipped += 1
            row.detail = f"verdict={verdict}" if entry.get("tool") != "replay" else "prior replay output"
            report.rows.append(row)
            continue

        report.executed_entries += 1

        # dry-run always analyzes; execute mode skips analysis-only work
        try:
            kind_now, allowed_now = _classify_now(sql)
        except Exception as exc:  # noqa: BLE001 - analysis failure is a divergence signal
            kind_now, allowed_now = "error", False
            row.detail = f"classification error: {exc}"

        original_class = entry.get("classification", "")
        divergent = kind_now != original_class

        if not execute:
            if divergent:
                row.status = "divergent"
                row.detail = f"classified {original_class} then, {kind_now} now"
                report.divergent += 1
            elif not allowed_now:
                row.status = "divergent"
                row.detail = "current guardrails would refuse this statement"
                report.divergent += 1
            else:
                row.status = "would-execute"
                report.replayed += 1
            report.rows.append(row)
            continue

        # --execute: run it for real on the readwrite pool
        try:
            pool = await conn_manager.readwrite_pool()
            async with pool.acquire() as conn:
                await conn.execute(sql)
            row.status = "replayed"
            report.replayed += 1
            replay_audit.record(
                AuditEntry(
                    tool="replay",
                    sql=sql,
                    verdict="executed",
                    classification=kind_now,
                    detail=f"replay of {entry.get('audit_id', '?')} (original actor: {row.actor})",
                    actor=f"replay:{row.actor}",
                )
            )
        except PgopsError as exc:
            row.status = "failed"
            row.detail = exc.message
            report.failed_at = entry.get("audit_id", "?")
            report.rows.append(row)
            break  # partial replay beats silent divergence
        except Exception as exc:  # noqa: BLE001 - record and stop
            row.status = "failed"
            row.detail = str(exc)
            report.failed_at = entry.get("audit_id", "?")
            report.rows.append(row)
            break

        report.rows.append(row)

    return report


def print_report(report: ReplayReport) -> None:
    print(report.summary())
    shown = 0
    for row in report.rows:
        if row.status == "skipped" and shown > 20:
            continue
        marker = {
            "replayed": "+",
            "would-execute": "~",
            "divergent": "!",
            "failed": "X",
            "skipped": ".",
        }.get(row.status, "?")
        sql_short = row.sql.replace("\n", " ")[:70]
        print(f"  [{marker}] {row.status:<13} {row.original_verdict:<12} {sql_short}")
        if row.detail:
            print(f"      {row.detail}")
        shown += 1


async def run_replay(
    config: Any,
    audit_path: Path,
    *,
    execute: bool,
    actor_filter: str | None = None,
    limit: int | None = None,
) -> ReplayReport:
    """Entry point for the CLI: builds its own ConnectionManager when executing."""
    if not execute:
        # dry-run needs no pools; pass a manager that will never be used
        class _NullManager:
            readwrite_pool = None

        return await replay_audit_log(
            _NullManager(),  # type: ignore[arg-type]
            audit_path,
            execute=False,
            actor_filter=actor_filter,
            limit=limit,
        )

    manager = ConnectionManager(config)
    await manager.start()
    try:
        return await replay_audit_log(
            manager,
            audit_path,
            execute=True,
            actor_filter=actor_filter,
            limit=limit,
        )
    finally:
        await manager.stop()


# CLI helper so __main__ stays thin
def confirm_execute() -> bool:
    answer = input(
        "Replaying EXECUTES every previously-executed statement against the target "
        "database.\nThis is destructive and non-idempotent. Type REPLAY to continue: "
    )
    return answer.strip() == "REPLAY"


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(run_replay(None, Path("~/.pgops/audit.jsonl").expanduser(), execute=False))
