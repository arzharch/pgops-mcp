"""migration.plan / apply / rollback (Phase 4).

`plan` is pure analysis — it computes a diff, annotates every step with lock impact, and
optionally dry-runs the whole thing inside a transaction that is always rolled back.
Nothing is applied. The plan is returned with an id and a checksum so `apply` can verify
it is executing exactly what the user approved.

`apply` is the dangerous one, and the ordering reflects that:

    re-plan → checksum match → crash check → confirmation token → ledger begin
      → execute → ledger finish

Re-planning rather than trusting the plan the caller handed back is deliberate. A plan
is a snapshot of a schema that other people can change; executing a stale plan is how a
migration tool drops a column someone else already replaced. The checksum comparison
turns "the schema moved under you" into a refusal instead of a surprise.

Transactionality: Postgres is transactional for most DDL, so the default is to run every
step in one transaction — either the whole migration lands or none of it does. The
exception is `CREATE INDEX CONCURRENTLY`, which *cannot* run inside a transaction block
(verified, not assumed). Those steps run outside the transaction, which means a
migration containing them is not atomic — so the plan says so explicitly rather than
letting the caller assume a guarantee that does not hold.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from pgops.audit import AuditEntry, AuditLog
from pgops.config import PgopsConfig
from pgops.connections import ConnectionManager
from pgops.errors import ErrorCode, PgopsError
from pgops.guardrails import ConfirmationTokenStore
from pgops.migrations.diff import Change, ChangeSet, diff_schema
from pgops.migrations.ledger import (
    MigrationLedger,
    MigrationStatus,
    checksum_steps,
)
from pgops.migrations.lock_analysis import LockImpact, analyze_statement
from pgops.timing import Elapsed
from pgops.tools.schema import schema_inspect


@dataclass(slots=True)
class PlanStep:
    change: Change
    impact: LockImpact
    dry_run: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {**self.change.to_dict(), "lock_impact": self.impact.to_dict()}
        if self.dry_run:
            d["dry_run"] = self.dry_run
        return d


@dataclass(slots=True)
class MigrationPlan:
    plan_id: str
    checksum: str
    steps: list[PlanStep]
    notes: list[str] = field(default_factory=list)
    dry_run_ok: bool | None = None
    atomic: bool = True
    # The inputs that produced this plan, kept so `apply` can re-diff against the LIVE
    # schema and detect that it moved since planning. Without them, apply can only re-hash
    # its own cached steps — an integrity check against nothing.
    target: dict[str, Any] = field(default_factory=dict)
    allow_drops: bool = False

    @property
    def sql_steps(self) -> list[str]:
        return [s.change.sql for s in self.steps]

    @property
    def highest_risk(self) -> str:
        order = {"low": 0, "medium": 1, "high": 2, "unknown": 3}
        if not self.steps:
            return "low"
        return max((s.impact.risk for s in self.steps), key=lambda r: order[r])

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "checksum": self.checksum,
            "atomic": self.atomic,
            "highest_risk": self.highest_risk,
            "destructive": any(s.change.destructive for s in self.steps),
            "steps": [s.to_dict() for s in self.steps],
            "dry_run_ok": self.dry_run_ok,
            "notes": self.notes,
        }


# Plans are held in memory for the life of the server, like confirmation tokens: a plan
# that outlives the process describes a schema nobody has re-verified.
_PLAN_CACHE: dict[str, MigrationPlan] = {}


async def _table_row_counts(conn_manager: ConnectionManager) -> dict[str, int]:
    snapshot = await schema_inspect(conn_manager, level="summary")
    return {t.name: t.estimated_rows for t in snapshot.tables}


async def _build_plan(
    conn_manager: ConnectionManager, target: dict[str, Any], allow_drops: bool
) -> tuple[ChangeSet, list[PlanStep]]:
    live = (await schema_inspect(conn_manager, level="full")).to_dict("full")
    changeset = diff_schema(live, target, allow_drops=allow_drops)
    rows_by_table = await _table_row_counts(conn_manager)
    steps = [
        PlanStep(
            change=change,
            impact=analyze_statement(change.sql, rows_by_table.get(change.table, 0)),
        )
        for change in changeset.changes
    ]
    return changeset, steps


async def migration_plan(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    target: dict[str, Any],
    allow_drops: bool = False,
    dry_run: bool = True,
) -> MigrationPlan:
    changeset, steps = await _build_plan(conn_manager, target, allow_drops)

    non_transactional = [s for s in steps if not s.impact.transactional]
    plan = MigrationPlan(
        plan_id=secrets.token_urlsafe(12),
        checksum=checksum_steps([s.change.sql for s in steps]),
        steps=steps,
        notes=list(changeset.notes),
        atomic=not non_transactional,
        target=target,
        allow_drops=allow_drops,
    )

    if non_transactional:
        plan.notes.append(
            f"{len(non_transactional)} step(s) cannot run inside a transaction "
            "(CREATE INDEX CONCURRENTLY), so this migration is NOT atomic — if a later "
            "step fails, earlier ones stay applied. Consider applying them separately."
        )

    if not steps:
        plan.notes.append("database already matches the target schema; nothing to do")
        _PLAN_CACHE[plan.plan_id] = plan
        return plan

    if dry_run:
        plan.dry_run_ok = await _dry_run(conn_manager, plan)

    _PLAN_CACHE[plan.plan_id] = plan
    return plan


async def _dry_run(conn_manager: ConnectionManager, plan: MigrationPlan) -> bool:
    """Execute every transactional step inside a transaction, then always roll back.

    This catches what static analysis cannot: a type that does not exist, a constraint
    that existing data violates, a column referenced in an index that was never added.
    Those are exactly the failures that would otherwise surface halfway through a real
    apply, with earlier steps already committed.
    """

    class _Rollback(Exception):
        pass

    pool = await conn_manager.readwrite_pool()
    try:
        async with pool.acquire() as conn, conn.transaction():
            for step in plan.steps:
                if not step.impact.transactional:
                    step.dry_run = "skipped: cannot run inside a transaction"
                    continue
                try:
                    await conn.execute(step.change.sql)
                    step.dry_run = "ok"
                except asyncpg.PostgresError as exc:
                    step.dry_run = f"would fail: {exc}"
                    raise _Rollback from exc
            raise _Rollback
    except _Rollback:
        pass
    return all(s.dry_run in ("ok", "skipped: cannot run inside a transaction") for s in plan.steps)


async def migration_apply(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
    plan_id: str,
    confirm_token: str | None = None,
    name: str = "unnamed",
) -> dict[str, Any]:
    plan = _PLAN_CACHE.get(plan_id)
    if plan is None:
        raise PgopsError(
            ErrorCode.INVALID_ARGUMENT,
            f"unknown plan_id {plan_id!r}",
            hint="plans live in memory for the life of the server; call migration.plan again",
        )
    if not plan.steps:
        return {"applied": False, "reason": "nothing to do", "steps": 0}

    pool = await conn_manager.readwrite_pool()
    async with pool.acquire() as conn:
        ledger = MigrationLedger(conn)
        await ledger.ensure_table()

        # Idempotency comes before the drift check: re-applying a plan that already landed
        # is a no-op, not "stale" — even though re-diffing would report drift, because the
        # plan is what changed the schema in the first place.
        already = await ledger.get_applied(plan_id)
        if already:
            return {"applied": False, "reason": "already applied", "plan_id": plan_id}

        stranded = await ledger.find_in_flight()
        if stranded:
            raise PgopsError(
                ErrorCode.MIGRATION_IN_FLIGHT,
                f"a previous migration ({stranded[0].migration_id}) is still marked "
                "in_flight — it was interrupted and the database may be half-migrated",
                hint=(
                    f"inspect the schema, then close the row with migration.resolve"
                    f"(ledger_id={stranded[0].id}, outcome, note). Nothing else applies "
                    f"until you do."
                ),
            )

    # Re-plan against the LIVE schema and compare. A plan is a snapshot of a schema other
    # people can change; executing a stale plan is how a migration tool drops a column
    # someone else already replaced. The old code here re-hashed the cached plan's own
    # steps against its own checksum, which can only ever match — it never touched the
    # database, so the documented "the schema moved under you" refusal did not exist.
    # This re-diffs the stored target against the current schema and refuses on a
    # mismatch. It catches structural drift (a column you were adding now exists, a type
    # changed); it cannot catch a column that was dropped and re-added with an identical
    # definition but different data, because that is invisible to a schema diff — see the
    # known-limits note in SECURITY.md.
    try:
        _, fresh_steps = await _build_plan(conn_manager, plan.target, plan.allow_drops)
    except PgopsError:
        raise
    except Exception as exc:
        raise PgopsError(
            ErrorCode.MIGRATION_FAILED,
            f"could not re-verify the plan against the current schema: {exc}",
            hint="the schema may have changed; call migration.plan again",
        ) from exc
    if checksum_steps([s.change.sql for s in fresh_steps]) != plan.checksum:
        raise PgopsError(
            ErrorCode.MIGRATION_STALE,
            "the database schema has changed since this plan was created; the plan no "
            "longer matches what the migration would do now",
            hint="another change landed between plan and apply. Call migration.plan again "
            "to see the current diff, then apply the fresh plan.",
        )

    # Confirmation gate — destructive steps or anything high-risk needs explicit sign-off.
    needs_confirmation = (
        any(s.change.destructive for s in plan.steps) or plan.highest_risk == "high"
    )
    approval_subject = f"migration:{plan_id}:{plan.checksum}"
    if needs_confirmation:
        if confirm_token is None:
            reason = _confirmation_reason(plan)
            token = tokens.issue(approval_subject, reason)
            audit.record(
                AuditEntry(
                    tool="migration.apply",
                    sql="; ".join(plan.sql_steps),
                    verdict="refused_pending_confirmation",
                    classification="migration",
                    detail=reason,
                )
            )
            raise PgopsError(
                ErrorCode.CONFIRMATION_REQUIRED,
                reason,
                hint=f"call migration.apply again with confirm_token={token!r}",
            )
        try:
            tokens.redeem(confirm_token, approval_subject)
        except PgopsError as exc:
            audit.record(
                AuditEntry(
                    tool="migration.apply",
                    sql="; ".join(plan.sql_steps),
                    verdict="refused_bad_token",
                    classification="migration",
                    error_code=exc.code.value,
                    detail=exc.message,
                )
            )
            raise

    return await _execute_plan(conn_manager, audit, plan, name)


def _confirmation_reason(plan: MigrationPlan) -> str:
    parts: list[str] = []
    destructive = [s for s in plan.steps if s.change.destructive]
    if destructive:
        losses = "; ".join(s.change.data_loss_reason or s.change.sql for s in destructive[:3])
        parts.append(f"{len(destructive)} destructive step(s): {losses}")
    risky = [s for s in plan.steps if s.impact.risk == "high"]
    for step in risky[:3]:
        parts.append(
            f"{step.change.sql[:70]} — {step.impact.reasoning.split('.')[0]} "
            f"(~{step.impact.estimate_ms}ms, {step.impact.confidence.value} confidence)"
        )
    if not plan.atomic:
        parts.append("this migration is NOT atomic (contains CONCURRENTLY steps)")
    return " | ".join(parts) or "migration requires confirmation"


async def _execute_plan(
    conn_manager: ConnectionManager, audit: AuditLog, plan: MigrationPlan, name: str
) -> dict[str, Any]:
    pool = await conn_manager.readwrite_pool()
    elapsed = Elapsed()
    completed: list[str] = []

    async with pool.acquire() as conn:
        ledger = MigrationLedger(conn)
        # Structured step records, not just SQL text: rollback needs kind/table/target
        # to invert each step faithfully. Parsing SQL text to recover the structure is
        # exactly the guessing this project refuses to do.
        step_details = [
            {
                "kind": s.change.kind.value,
                "table": s.change.table,
                "target": s.change.target,
                "sql": s.change.sql,
            }
            for s in plan.steps
        ]
        row_id = await ledger.begin(
            plan.plan_id,
            name,
            plan.checksum,
            plan.sql_steps,
            applied_by="pgops-mcp",
            step_details=step_details,
        )
        try:
            transactional = [s for s in plan.steps if s.impact.transactional]
            concurrent = [s for s in plan.steps if not s.impact.transactional]

            if transactional:
                async with conn.transaction():
                    for step in transactional:
                        await conn.execute(step.change.sql)
                        completed.append(step.change.sql)
            # CONCURRENTLY steps must run outside any transaction (verified against PG16)
            for step in concurrent:
                await conn.execute(step.change.sql)
                completed.append(step.change.sql)
        except (asyncpg.InterfaceError, ConnectionError, OSError) as exc:
            # The connection died mid-apply — the database restarted, the container was
            # killed, the network dropped. This is NOT asyncpg.PostgresError: an
            # InterfaceError is raised by the driver, not by Postgres, so it used to fall
            # through to the generic handler and reach the caller as
            # `INTERNAL_ERROR: internal error; see server logs`.
            #
            # That is the worst possible moment for an opaque error. The ledger row is
            # correctly left `in_flight` and the machinery to recover exists, but the
            # agent is the one holding the failure and it has just been told nothing —
            # not that a migration is half-applied, not that further applies will refuse,
            # not that migration.resolve is how it ends. Under stdio the human usually
            # cannot read the server log either.
            #
            # No ledger write is attempted: the connection carrying it is the one that
            # died, so that write would fail too and mask this error with a second one.
            # `in_flight` is the honest record of what happened, and it is already there.
            audit.record(
                AuditEntry(
                    tool="migration.apply",
                    sql="; ".join(plan.sql_steps),
                    verdict="interrupted",
                    classification="migration",
                    error_code=ErrorCode.MIGRATION_INTERRUPTED.value,
                    detail=f"{exc}; {len(completed)} step(s) completed before the "
                    f"connection was lost",
                )
            )
            raise PgopsError(
                ErrorCode.MIGRATION_INTERRUPTED,
                f"the database connection was lost while applying {name!r} "
                f"({len(completed)} of {len(plan.steps)} step(s) had completed): {exc}",
                hint=(
                    f"pgops cannot tell whether the work committed, so ledger row "
                    f"{row_id} stays in_flight and further migrations will refuse. "
                    f"Inspect the schema with schema.inspect, then close it with "
                    f"migration.resolve(ledger_id={row_id}, outcome, note)."
                ),
            ) from exc
        except asyncpg.PostgresError as exc:
            await ledger.fail(row_id, str(exc))
            audit.record(
                AuditEntry(
                    tool="migration.apply",
                    sql="; ".join(plan.sql_steps),
                    verdict="failed",
                    classification="migration",
                    error_code=ErrorCode.MIGRATION_FAILED.value,
                    detail=f"{exc}; {len(completed)} step(s) completed before failure",
                )
            )
            raise PgopsError(
                ErrorCode.MIGRATION_FAILED,
                f"migration failed: {exc}",
                hint=(
                    f"{len(completed)} step(s) had completed. "
                    + (
                        "All transactional steps were rolled back."
                        if plan.atomic
                        else "This migration was NOT atomic — inspect the schema."
                    )
                ),
            ) from exc

        duration_ms = elapsed.ms
        await ledger.finish(row_id, duration_ms)

    audit.record(
        AuditEntry(
            tool="migration.apply",
            sql="; ".join(plan.sql_steps),
            verdict="executed",
            classification="migration",
            duration_ms=round(duration_ms, 2),
            rows_affected=len(completed),
        )
    )
    return {
        "applied": True,
        "plan_id": plan.plan_id,
        "steps_applied": len(completed),
        "duration_ms": round(duration_ms, 2),
        "atomic": plan.atomic,
    }


async def migration_history(conn_manager: ConnectionManager, limit: int = 20) -> dict[str, Any]:
    # Validate before it reaches SQL. A negative limit makes Postgres raise
    # "LIMIT must not be negative", which is a PostgresError the tool does not anticipate
    # and so surfaces as an opaque INTERNAL_ERROR — found by fuzzing during sign-off. Bound
    # it to a sane range instead, with a clear message on the low end and a silent cap on
    # the high end (the ledger is not something a caller needs ten-thousand rows of).
    if limit < 1:
        raise PgopsError(
            ErrorCode.INVALID_ARGUMENT,
            f"limit must be at least 1, got {limit}",
            hint="omit limit for the default of 20",
        )
    limit = min(limit, 1000)
    pool = await conn_manager.readwrite_pool()
    async with pool.acquire() as conn:
        ledger = MigrationLedger(conn)
        await ledger.ensure_table()
        entries = await ledger.history(limit)
        in_flight = await ledger.find_in_flight()
    return {
        "history": [e.to_dict() for e in entries],
        "in_flight": [e.to_dict() for e in in_flight],
        "warning": (
            "a migration is marked in_flight — it was interrupted and the schema may "
            "be half-migrated. Inspect the schema, then close the row with "
            "migration.resolve(ledger_id, outcome, note); further migrations refuse "
            "until you do."
            if in_flight
            else None
        ),
    }


async def migration_describe(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    description: str,
    allow_drops: bool = False,
    dry_run: bool = True,
    ctx: Any = None,
) -> dict[str, Any]:
    """Turn a natural-language schema change into a real, analysed plan.

    The friction in the migration workflow is not the analysis — it is writing the
    `target` object by hand, which describes the desired *final state* rather than the
    change, so it has to restate columns the table already has. That is a mechanical
    translation, and it is what MCP sampling is for: the **client's** model does it, so
    this server needs no API key and no model of its own.

    The safety argument is that the model's output is not trusted anywhere:

        description -> [client's model] -> target JSON -> _validate_target
                    -> deterministic differ -> lock analysis -> dry run -> plan

    The model proposes a *destination*. It never writes SQL, and every statement is
    still generated by the same differ, annotated by the same lock analysis and gated by
    the same confirmation flow a hand-written target would face. If the model
    hallucinates a column the plan will show an ADD COLUMN the user can see and reject;
    it cannot smuggle a statement past a guardrail, because it never produces one.

    Returns the sampled target alongside the plan so the user reviews the translation,
    not just its consequences.
    """
    from pgops.sampling import TARGET_SYSTEM_PROMPT, sample_json

    live = await schema_inspect(conn_manager, level="full")
    current = live.to_dict("full")

    target = await sample_json(
        ctx,
        f"Requested change:\n{description}\n\n"
        f"Current schema:\n{json.dumps(current, indent=2)[:20000]}",
        TARGET_SYSTEM_PROMPT,
    )

    if target is None:
        # Sampling is optional in MCP and plenty of clients lack it. Failing with the
        # equivalent hand-written call is more useful than an apology.
        raise PgopsError(
            ErrorCode.SAMPLING_UNAVAILABLE,
            "this client does not support sampling, so the description could not be "
            "translated into a target schema",
            hint=(
                "call migration.plan directly with an explicit target, e.g. "
                '{"tables": {"orders": {"columns": {"note": {"type": "text"}}}}}'
            ),
        )
    if "error" in target and "tables" not in target:
        # The prompt tells the model to say so rather than guess. Passing that refusal
        # through beats planning a migration from an invented interpretation.
        raise PgopsError(
            ErrorCode.INVALID_ARGUMENT,
            f"the request could not be translated: {target['error']}",
            hint="rephrase naming the table and columns explicitly",
        )

    plan = await migration_plan(
        conn_manager, config, target, allow_drops=allow_drops, dry_run=dry_run
    )
    return {
        # Surfaced deliberately: the user should review the *interpretation*, not only
        # the SQL it produced. A wrong target that happens to generate valid SQL is the
        # failure mode worth catching here.
        "interpreted_target": target,
        "description": description,
        **plan.to_dict(),
    }


async def migration_resolve(
    conn_manager: ConnectionManager,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
    ledger_id: int,
    outcome: str,
    note: str,
    confirm_token: str | None = None,
) -> dict[str, Any]:
    """Close out an interrupted (in_flight) migration after a human has established what
    actually happened to the schema.

    A crash between recording intent and recording the result — the database going away
    mid-DDL, the server being killed, the container restarting — leaves the ledger row
    `in_flight`, and every later apply refuses while one exists. That refusal is correct:
    pgops genuinely cannot tell whether the DDL committed, and guessing is the thing this
    project exists not to do.

    What was missing was the way out. The advice attached to the refusal was "resolve the
    row manually", meaning: open psql and UPDATE the ledger by hand. So the first crash
    reduced an MCP server whose purpose is letting an agent operate the database to
    "connect to the database yourself" — permanently, until someone did. Verified by
    SIGKILLing Postgres mid-index-build: the pool recovered, queries resumed, and every
    subsequent migration.apply refused.

    This tool does not inspect the schema and does not decide anything. The caller
    inspects, decides, and states the outcome; pgops records it verbatim with the note
    and the caller's identity, behind a confirmation token, in both the ledger and the
    audit log. `applied` and `failed` are the only outcomes offered, because those are
    the only two things that can have happened.
    """
    if outcome not in {"applied", "failed"}:
        raise PgopsError(
            ErrorCode.INVALID_ARGUMENT,
            f"outcome must be 'applied' or 'failed', not {outcome!r}",
            hint="'applied' if the schema shows the change landed, 'failed' if it did "
            "not. Inspect with schema.inspect before deciding.",
        )
    if not note.strip():
        raise PgopsError(
            ErrorCode.INVALID_ARGUMENT,
            "note is required",
            hint="record what you checked and what you concluded — this is the only "
            "explanation anyone reading the ledger later will have",
        )

    pool = await conn_manager.readwrite_pool()
    async with pool.acquire() as conn:
        ledger = MigrationLedger(conn)
        await ledger.ensure_table()
        entry = await ledger.get_by_id(ledger_id)
        if entry is None:
            raise PgopsError(
                ErrorCode.INVALID_ARGUMENT,
                f"no migration with ledger id {ledger_id}",
                hint="run migration.history to see recorded migrations",
            )
        if entry.status is not MigrationStatus.IN_FLIGHT:
            raise PgopsError(
                ErrorCode.INVALID_ARGUMENT,
                f"migration {entry.migration_id!r} has status {entry.status.value!r}; "
                "only an interrupted (in_flight) migration needs resolving",
            )

        subject = f"migration.resolve:{entry.migration_id}:{outcome}"
        reason = (
            f"recording interrupted migration {entry.name!r} as {outcome!r}. This is a "
            f"statement about what the schema already contains — it changes the ledger, "
            f"not the database, and it unblocks further migrations."
        )
        if confirm_token is None:
            token = tokens.issue(subject, reason)
            audit.record(
                AuditEntry(
                    tool="migration.resolve",
                    sql=f"resolve:{entry.migration_id}:{outcome}",
                    verdict="refused_pending_confirmation",
                    classification="migration",
                    detail=reason,
                )
            )
            raise PgopsError(
                ErrorCode.CONFIRMATION_REQUIRED,
                reason,
                hint=f"call migration.resolve again with confirm_token={token!r}",
            )
        tokens.redeem(confirm_token, subject)

        resolved = await ledger.resolve_in_flight(ledger_id, outcome, note.strip())
        audit.record(
            AuditEntry(
                tool="migration.resolve",
                sql=f"resolve:{entry.migration_id}:{outcome}",
                verdict="executed",
                classification="migration",
                detail=note.strip(),
            )
        )
    return {
        "resolved": resolved is not None,
        "ledger_id": ledger_id,
        "migration_id": entry.migration_id,
        "status": outcome,
        "note": note.strip(),
    }
