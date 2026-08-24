"""Reversibility analysis: can an applied migration actually be undone? (PRD FR-3)

This was the last tool left unbuilt, deliberately, because the naive version is
dangerous in a way that is easy to miss. "Rollback" sounds like an undo button. For
schema migrations it is not one, and a tool that implies otherwise will eventually be
trusted by an agent at 3am to reverse something that cannot be reversed.

The distinction that matters is **schema reversibility versus data reversibility**:

    ADD COLUMN      -> DROP COLUMN     schema restored, but every value written to that
                                       column since is destroyed
    CREATE INDEX    -> DROP INDEX      genuinely reversible; an index is derived data
    DROP COLUMN     -> (nothing)       the values are gone. There is no inverse. Not a
                                       hard one, not a slow one — none.

So each recorded step is classified, and the honest outcomes are three, not two:
reversible, reversible-but-destroys-data-written-since, and irreversible.

**Any irreversible step refuses the whole rollback.** Doing the reversible half would
leave the schema in a state neither the migration nor the rollback describes — worse
than either, and harder to reason about during the incident that prompted the rollback.
The refusal names the offending step and points at the only thing that actually works
(a restore), rather than doing something that looks like progress.

Two further guards:

- Steps are reversed in **reverse order**. Forward order creates the table before the
  index; the inverse must drop the index before the table.
- A migration with later applied migrations stacked on top is refused. Reversing an
  earlier change under a later one that depends on it produces failures that are hard
  to unwind, and picking that fight silently is not this tool's call to make.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import asyncpg

from pgops.audit import AuditEntry, AuditLog
from pgops.connections import ConnectionManager
from pgops.errors import ErrorCode, PgopsError
from pgops.guardrails import ConfirmationTokenStore
from pgops.migrations.diff import ChangeKind, _quote_ident
from pgops.migrations.ledger import MigrationLedger, MigrationStatus
from pgops.timing import Elapsed


class Reversibility(StrEnum):
    FULL = "reversible"
    DATA_LOSS = "reversible_with_data_loss"
    NONE = "irreversible"


@dataclass(slots=True)
class Reversal:
    """The inverse of one applied step, or an explanation of why there isn't one."""

    original_sql: str
    kind: str
    reversibility: Reversibility
    sql: str | None = None
    reason: str = ""

    @property
    def blocks_rollback(self) -> bool:
        return self.reversibility is Reversibility.NONE

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "original_sql": self.original_sql,
            "reversibility": self.reversibility.value,
            "reason": self.reason,
        }
        if self.sql:
            payload["sql"] = self.sql
        return payload


def invert(step: dict[str, Any]) -> Reversal:
    """Compute the inverse of one recorded migration step."""
    kind = step.get("kind", "")
    table = step.get("table", "")
    target = step.get("target", "")
    original = step.get("sql", "")
    quoted_table = _quote_ident(table) if table else ""

    def rev(reversibility: Reversibility, reason: str, sql: str | None = None) -> Reversal:
        return Reversal(
            original_sql=original, kind=kind, reversibility=reversibility, sql=sql, reason=reason
        )

    if kind == ChangeKind.CREATE_INDEX.value:
        # The clean case, and the reason it is clean is worth naming: an index holds no
        # information of its own. Dropping it destroys only derived data, and the
        # original CREATE statement rebuilds it exactly.
        return rev(
            Reversibility.FULL,
            "an index is derived data; dropping it loses nothing that cannot be rebuilt",
            f"DROP INDEX IF EXISTS {_quote_ident(target)}" if target else None,
        )

    if kind == ChangeKind.ADD_CONSTRAINT.value:
        return rev(
            Reversibility.FULL,
            "dropping a constraint removes a rule, not data",
            f"ALTER TABLE {quoted_table} DROP CONSTRAINT IF EXISTS {_quote_ident(target)}",
        )

    if kind == ChangeKind.SET_NOT_NULL.value:
        return rev(
            Reversibility.FULL,
            "relaxing a NOT NULL cannot fail and destroys nothing",
            f"ALTER TABLE {quoted_table} ALTER COLUMN {_quote_ident(target)} DROP NOT NULL",
        )

    if kind == ChangeKind.DROP_NOT_NULL.value:
        # Re-imposing NOT NULL fails outright if a NULL was written in the meantime.
        # Postgres refusing is the safe outcome — no data is touched — so this counts as
        # reversible, with the caveat stated rather than discovered.
        return rev(
            Reversibility.FULL,
            "re-imposing NOT NULL will fail (harmlessly) if any NULL was written since",
            f"ALTER TABLE {quoted_table} ALTER COLUMN {_quote_ident(target)} SET NOT NULL",
        )

    if kind == ChangeKind.ADD_COLUMN.value:
        return rev(
            Reversibility.DATA_LOSS,
            f"the schema reverts, but every value written to {table}.{target} since the "
            "migration is destroyed",
            f"ALTER TABLE {quoted_table} DROP COLUMN IF EXISTS {_quote_ident(target)}",
        )

    if kind == ChangeKind.CREATE_TABLE.value:
        return rev(
            Reversibility.DATA_LOSS,
            f"the schema reverts, but every row inserted into {table} since the migration "
            "is destroyed",
            f"DROP TABLE IF EXISTS {quoted_table}",
        )

    if kind == ChangeKind.ALTER_COLUMN_TYPE.value:
        previous = step.get("previous")
        if not previous:
            # Without the pre-migration type there is nothing to convert back *to*, and
            # guessing it from the current type is exactly the kind of confident wrong
            # answer this tool exists to avoid.
            return rev(
                Reversibility.NONE,
                f"the previous type of {table}.{target} was not recorded, so there is no "
                "inverse to generate",
            )
        return rev(
            Reversibility.DATA_LOSS,
            f"converting {table}.{target} back to {previous} may round or truncate values "
            "written since (numeric scale and timestamp precision are lost silently)",
            f"ALTER TABLE {quoted_table} ALTER COLUMN {_quote_ident(target)} TYPE {previous}",
        )

    if kind == ChangeKind.DROP_COLUMN.value:
        return rev(
            Reversibility.NONE,
            f"{table}.{target} was dropped; the values are gone and re-adding the column "
            "produces NULLs, not the original data",
        )

    if kind == ChangeKind.DROP_TABLE.value:
        return rev(
            Reversibility.NONE,
            f"{table} was dropped with all of its rows; re-creating it produces an empty "
            "table, not the original one",
        )

    if kind in {ChangeKind.DROP_INDEX.value, ChangeKind.DROP_CONSTRAINT.value}:
        # Recoverable in principle — the definition is a string — but only if it was
        # captured before the drop, and it was not. Saying so beats emitting a CREATE
        # statement reconstructed from assumptions about the original.
        return rev(
            Reversibility.NONE,
            f"the definition of {target or 'the dropped object'} was not captured before "
            "it was dropped, so it cannot be recreated faithfully",
        )

    return rev(
        Reversibility.NONE,
        f"step kind {kind!r} has no known inverse; it is treated as irreversible rather "
        "than guessed at",
    )


@dataclass(slots=True)
class RollbackPlan:
    migration_id: str
    reversals: list[Reversal]

    @property
    def blocked_by(self) -> list[Reversal]:
        return [r for r in self.reversals if r.blocks_rollback]

    @property
    def possible(self) -> bool:
        return bool(self.reversals) and not self.blocked_by

    @property
    def destroys_data(self) -> bool:
        return any(r.reversibility is Reversibility.DATA_LOSS for r in self.reversals)

    @property
    def sql_steps(self) -> list[str]:
        return [r.sql for r in self.reversals if r.sql]

    def to_dict(self) -> dict[str, Any]:
        return {
            "migration_id": self.migration_id,
            "possible": self.possible,
            "destroys_data": self.destroys_data,
            "steps": [r.to_dict() for r in self.reversals],
            "blocked_by": [r.to_dict() for r in self.blocked_by],
        }


def plan_rollback(migration_id: str, steps: list[dict[str, Any]]) -> RollbackPlan:
    """Invert an applied migration's recorded steps, last one first.

    Reverse order is not cosmetic: the forward plan creates a table before indexing it,
    so the inverse has to drop the index before the table or the drop fails on a
    dependency.
    """
    return RollbackPlan(migration_id=migration_id, reversals=[invert(s) for s in reversed(steps)])


async def rollback_migration(
    conn_manager: ConnectionManager,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
    row_id: int,
    confirm_token: str | None = None,
) -> dict[str, Any]:
    """Reverse an applied migration, with the same gating as every destructive path.

    The gates, in order:

    1. The ledger row must exist and be `applied` — there is nothing to reverse
       otherwise, and a failed or in-flight row describes a state neither the forward
       migration nor any inverse accounts for.
    2. **Stack check.** A later applied migration blocks this one. Reversing an earlier
       change under a later one that may depend on it produces failures that are hard
       to unwind; refusing names the blocker instead of discovering it mid-rollback.
    3. **Irreversibility refusal.** Any irreversible step refuses the *whole* rollback.
       Doing the reversible half would leave the schema in a state neither the migration
       nor the rollback describes — worse than either during the incident that prompted
       this. This raises MIGRATION_IRREVERSIBLE and issues no token: there is no version
       of "yes" that makes DROP COLUMN's data come back.
    4. **Confirmation token** bound to the migration id + checksum, because a rollback
       that destroys data written since (DROP COLUMN of an added column, table drop)
       is exactly as dangerous as the forward migration was.

    Execution runs in one transaction — every reversal here is transactional DDL by
    construction (CONCURRENTLY steps are never recorded as reversible), so all-or-
    nothing holds. On success the ledger row becomes `rolled_back`, which keeps history
    honest without pretending the data came back.
    """
    pool = await conn_manager.readwrite_pool()
    async with pool.acquire() as conn:
        ledger = MigrationLedger(conn)
        await ledger.ensure_table()

        entry = await ledger.get_by_id(row_id)
        if entry is None:
            raise PgopsError(
                ErrorCode.INVALID_ARGUMENT,
                f"no migration with ledger id {row_id}",
                hint="run migration.history to see recorded migrations",
            )
        if entry.status is not MigrationStatus.APPLIED:
            raise PgopsError(
                ErrorCode.INVALID_ARGUMENT,
                f"migration {entry.migration_id!r} has status {entry.status.value!r}; "
                "only applied migrations can be rolled back",
            )

        stacked = await ledger.applied_after(entry.started_at)
        if stacked:
            raise PgopsError(
                ErrorCode.MIGRATION_IN_FLIGHT,
                f"{len(stacked)} migration(s) were applied after "
                f"{entry.migration_id!r} ({stacked[0].migration_id} is the earliest); "
                "rolling back underneath them could break their assumptions",
                hint="roll back the later migrations first, or resolve manually",
            )

        if not entry.steps or not all(isinstance(s, dict) for s in entry.steps):
            raise PgopsError(
                ErrorCode.MIGRATION_IRREVERSIBLE,
                f"migration {entry.migration_id!r} has no structured step record, so its "
                "steps cannot be inverted faithfully",
                hint=(
                    "migrations applied before structured recording only stored SQL text; "
                    "write the reversal explicitly instead"
                ),
            )

        plan = plan_rollback(entry.migration_id, entry.steps)

        if not plan.possible:
            blocked = plan.blocked_by[0]
            audit.record(
                AuditEntry(
                    tool="migration.rollback",
                    sql=f"rollback:{entry.migration_id}",
                    verdict="refused_irreversible",
                    classification="migration",
                    detail=blocked.reason,
                )
            )
            # No token is issued: unlike a risky-but-possible rollback, no human answer
            # changes what is possible. The honest output is the refusal itself.
            raise PgopsError(
                ErrorCode.MIGRATION_IRREVERSIBLE,
                f"cannot roll back {entry.migration_id!r}: {blocked.reason}",
                hint=(
                    "a restore from backup is the only way back to the pre-migration "
                    "state once data has been destroyed"
                ),
            )

        subject = f"migration.rollback:{entry.migration_id}:{entry.checksum}"
        reason = (
            f"rolling back {entry.name!r} ({len(plan.reversals)} step(s))"
            + (
                " — this DESTROYS data written since the migration was applied"
                if plan.destroys_data
                else ""
            )
        )
        if confirm_token is None:
            token = tokens.issue(subject, reason)
            audit.record(
                AuditEntry(
                    tool="migration.rollback",
                    sql=f"rollback:{entry.migration_id}",
                    verdict="refused_pending_confirmation",
                    classification="migration",
                    detail=reason,
                )
            )
            raise PgopsError(
                ErrorCode.CONFIRMATION_REQUIRED,
                reason,
                hint=f"call migration.rollback again with confirm_token={token!r}",
            )
        try:
            tokens.redeem(confirm_token, subject)
        except PgopsError as exc:
            audit.record(
                AuditEntry(
                    tool="migration.rollback",
                    sql=f"rollback:{entry.migration_id}",
                    verdict="refused_bad_token",
                    classification="migration",
                    error_code=exc.code.value,
                    detail=exc.message,
                )
            )
            raise

        elapsed = Elapsed()
        try:
            async with conn.transaction():
                for reversal in plan.reversals:
                    assert reversal.sql is not None  # guaranteed by plan.possible
                    await conn.execute(reversal.sql)
        except asyncpg.PostgresError as exc:
            audit.record(
                AuditEntry(
                    tool="migration.rollback",
                    sql=f"rollback:{entry.migration_id}",
                    verdict="failed",
                    classification="migration",
                    error_code=ErrorCode.MIGRATION_FAILED.value,
                    detail=str(exc),
                )
            )
            raise PgopsError(
                ErrorCode.MIGRATION_FAILED,
                f"rollback failed: {exc}",
                hint="the transaction rolled back; the original migration is still applied",
            ) from exc

        duration_ms = elapsed.ms
        await ledger.mark_rolled_back(entry.migration_id)

    audit.record(
        AuditEntry(
            tool="migration.rollback",
            sql=f"rollback:{entry.migration_id}",
            verdict="executed",
            classification="migration",
            duration_ms=round(duration_ms, 2),
            rows_affected=len(plan.reversals),
        )
    )
    return {
        "rolled_back": True,
        "migration_id": entry.migration_id,
        "name": entry.name,
        "steps_reversed": len(plan.reversals),
        "duration_ms": round(duration_ms, 2),
        "note": (
            "schema reverted; data written between apply and rollback did not come back"
            if plan.destroys_data
            else "no data was destroyed by these reversals"
        ),
    }
