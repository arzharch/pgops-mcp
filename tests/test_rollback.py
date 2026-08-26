"""migration.rollback against real Postgres (PRD FR-3).

The property under test is not "the inverse SQL runs" — it's that rollback refuses
exactly when it should: irreversible steps, stacked migrations, non-applied rows. A
rollback tool that executes when it shouldn't is worse than one that doesn't exist,
because it will be trusted at 3am to undo something it cannot.
"""

from __future__ import annotations

from typing import Any

import asyncpg
import pytest

from pgops.audit import AuditLog
from pgops.config import PgopsConfig
from pgops.connections import ConnectionManager
from pgops.errors import ErrorCode, PgopsError
from pgops.guardrails import ConfirmationTokenStore
from pgops.migrations.diff import ChangeKind
from pgops.migrations.ledger import LEDGER_TABLE, MigrationLedger
from pgops.migrations.rollback import (
    Reversibility,
    invert,
    plan_rollback,
    rollback_migration,
)
from pgops.tools.migrations import migration_apply, migration_plan


async def _columns(dsn: str, table: str) -> set[str]:
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=$1",
            table,
        )
        return {r["column_name"] for r in rows}
    finally:
        await conn.close()


async def _apply_migration(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
    target: dict[str, Any],
    name: str = "fwd",
) -> dict[str, Any]:
    """Apply a migration and return its result (includes plan_id but we need ledger id)."""
    plan = await migration_plan(conn_manager, config, target)
    return await migration_apply(conn_manager, config, audit, tokens, plan.plan_id, name=name)


async def _latest_ledger_id(conn_manager: ConnectionManager) -> int:
    pool = await conn_manager.readwrite_pool()
    async with pool.acquire() as conn:
        row_id: int | None = await conn.fetchval(
            f"SELECT id FROM {LEDGER_TABLE} ORDER BY id DESC LIMIT 1"
        )
    assert row_id is not None
    return row_id


async def _token_from(exc: PgopsError) -> str:
    return (exc.hint or "").split("confirm_token=")[1].split("'")[1]


# --- pure inversion logic ------------------------------------------------------------


def test_invert_add_column_is_data_loss() -> None:
    reversal = invert(
        {
            "kind": ChangeKind.ADD_COLUMN.value,
            "table": "orders",
            "target": "note",
            "sql": 'ALTER TABLE "orders" ADD COLUMN "note" text',
        }
    )
    assert reversal.reversibility is Reversibility.DATA_LOSS
    assert reversal.sql == 'ALTER TABLE "orders" DROP COLUMN IF EXISTS "note"'
    assert reversal.blocks_rollback is False


def test_invert_create_index_is_fully_reversible() -> None:
    reversal = invert(
        {
            "kind": ChangeKind.CREATE_INDEX.value,
            "table": "orders",
            "target": "idx_x",
            "sql": 'CREATE INDEX "idx_x" ON "orders" ("status")',
        }
    )
    assert reversal.reversibility is Reversibility.FULL
    assert reversal.sql == 'DROP INDEX IF EXISTS "idx_x"'


def test_invert_drop_column_is_irreversible() -> None:
    reversal = invert({"kind": ChangeKind.DROP_COLUMN.value, "table": "orders", "target": "status"})
    assert reversal.reversibility is Reversibility.NONE
    assert reversal.blocks_rollback is True
    assert reversal.sql is None


def test_invert_alter_type_without_previous_type_is_irreversible() -> None:
    """Without the pre-migration type there is nothing to convert back to."""
    reversal = invert(
        {
            "kind": ChangeKind.ALTER_COLUMN_TYPE.value,
            "table": "t",
            "target": "a",
            "sql": 'ALTER TABLE "t" ALTER COLUMN "a" TYPE bigint',
        }
    )
    assert reversal.reversibility is Reversibility.NONE


def test_plan_reverses_in_reverse_order() -> None:
    steps = [
        {"kind": ChangeKind.CREATE_TABLE.value, "table": "t", "sql": "CREATE TABLE t ()"},
        {"kind": ChangeKind.CREATE_INDEX.value, "table": "t", "target": "i", "sql": "..."},
    ]
    plan = plan_rollback("m1", steps)
    kinds = [r.kind for r in plan.reversals]
    # index must be dropped before the table it depends on
    assert kinds.index(ChangeKind.CREATE_INDEX.value) < kinds.index(ChangeKind.CREATE_TABLE.value)


def test_plan_blocked_when_any_step_irreversible() -> None:
    steps = [
        {"kind": ChangeKind.ADD_COLUMN.value, "table": "t", "target": "c"},
        {"kind": ChangeKind.DROP_COLUMN.value, "table": "t", "target": "old"},
    ]
    plan = plan_rollback("m1", steps)
    assert plan.possible is False
    assert len(plan.blocked_by) == 1


# --- live behaviour ------------------------------------------------------------------


async def test_rollback_reverts_a_safe_migration(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    target: dict[str, Any] = {"tables": {"items": {"columns": {"tag": {"type": "text"}}}}}
    await _apply_migration(conn_manager, config, audit, tokens, target, name="add-tag")
    assert "tag" in await _columns(config.dsn, "items")

    ledger_id = await _latest_ledger_id(conn_manager)
    with pytest.raises(PgopsError) as exc_info:
        await rollback_migration(conn_manager, audit, tokens, ledger_id)
    token = await _token_from(exc_info.value)

    result = await rollback_migration(conn_manager, audit, tokens, ledger_id, confirm_token=token)
    assert result["rolled_back"] is True
    assert "tag" not in await _columns(config.dsn, "items")


async def test_rollback_of_index_only_migration_destroys_nothing(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    """An index is derived data — its rollback should say so rather than hedge."""
    target: dict[str, Any] = {
        "tables": {"items": {"columns": {"name": {"type": "text"}}, "indexes": {"idx_nm": "name"}}}
    }
    await _apply_migration(conn_manager, config, audit, tokens, target)
    ledger_id = await _latest_ledger_id(conn_manager)

    with pytest.raises(PgopsError) as exc_info:
        await rollback_migration(conn_manager, audit, tokens, ledger_id)
    token = await _token_from(exc_info.value)
    result = await rollback_migration(conn_manager, audit, tokens, ledger_id, confirm_token=token)

    assert result["rolled_back"] is True
    assert "did not come back" not in result["note"]
    pool = await conn_manager.readwrite_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT count(*) FROM pg_indexes WHERE indexname = 'idx_nm'")
    assert count == 0


async def test_rollback_refuses_an_irreversible_migration_without_a_token(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    """DROP COLUMN cannot be undone by any human answer — so no token is ever issued.

    The refusal must come before the confirmation gate: a token here would imply there
    exists a version of "yes" that restores the data, and there isn't one.
    """
    target: dict[str, Any] = {"tables": {"items": {"columns": {"id": {"type": "integer"}}}}}
    plan = await migration_plan(conn_manager, config, target, allow_drops=True)
    # the forward apply is itself destructive — it needs its own confirmation first
    with pytest.raises(PgopsError) as apply_exc:
        await migration_apply(conn_manager, config, audit, tokens, plan.plan_id, name="drop-name")
    await migration_apply(
        conn_manager,
        config,
        audit,
        tokens,
        plan.plan_id,
        confirm_token=await _token_from(apply_exc.value),
        name="drop-name",
    )
    ledger_id = await _latest_ledger_id(conn_manager)

    with pytest.raises(PgopsError) as exc_info:
        await rollback_migration(conn_manager, audit, tokens, ledger_id)
    assert exc_info.value.code is ErrorCode.MIGRATION_IRREVERSIBLE
    assert tokens.outstanding() == 0  # no approval was minted for the impossible
    assert "name" not in await _columns(config.dsn, "items")  # refusal changed nothing


async def test_rollback_refuses_when_later_migrations_are_stacked(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    first: dict[str, Any] = {"tables": {"items": {"columns": {"a_one": {"type": "text"}}}}}
    await _apply_migration(conn_manager, config, audit, tokens, first, name="first")
    first_id = await _latest_ledger_id(conn_manager)

    second: dict[str, Any] = {"tables": {"items": {"columns": {"b_two": {"type": "text"}}}}}
    await _apply_migration(conn_manager, config, audit, tokens, second, name="second")

    with pytest.raises(PgopsError) as exc_info:
        await rollback_migration(conn_manager, audit, tokens, first_id)
    assert "applied after" in exc_info.value.message
    assert "b_two" in await _columns(config.dsn, "items")  # untouched


async def test_rollback_refuses_a_non_applied_row(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    pool = await conn_manager.readwrite_pool()
    async with pool.acquire() as conn:
        ledger = MigrationLedger(conn)
        await ledger.ensure_table()
        failed_id = await ledger.begin("failed-one", "n", "sum", [], "test")
        await ledger.fail(failed_id, "boom")

    with pytest.raises(PgopsError) as exc_info:
        await rollback_migration(conn_manager, audit, tokens, failed_id)
    assert "failed" in exc_info.value.message

    async with pool.acquire() as conn:
        await conn.execute(f"DELETE FROM {LEDGER_TABLE} WHERE migration_id = 'failed-one'")


async def test_rollback_unknown_ledger_id_is_structured_error(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    with pytest.raises(PgopsError) as exc_info:
        await rollback_migration(conn_manager, audit, tokens, 999999)
    assert exc_info.value.code is ErrorCode.INVALID_ARGUMENT


async def test_rollback_token_is_bound_to_the_migration(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    """Approval to roll back one migration must not reverse a different one."""
    target: dict[str, Any] = {"tables": {"items": {"columns": {"x_col": {"type": "text"}}}}}
    await _apply_migration(conn_manager, config, audit, tokens, target, name="add-x")
    ledger_id = await _latest_ledger_id(conn_manager)

    with pytest.raises(PgopsError) as exc_info:
        await rollback_migration(conn_manager, audit, tokens, ledger_id)
    token = await _token_from(exc_info.value)

    # redeem it against a different migration's subject -> mismatch, column survives
    other_subject = "migration.rollback:some-other-migration:abc"
    with pytest.raises(PgopsError) as exc_mismatch:
        tokens.redeem(token, other_subject)
    assert exc_mismatch.value.code is ErrorCode.CONFIRMATION_MISMATCH

    result = await rollback_migration(conn_manager, audit, tokens, ledger_id, confirm_token=token)
    assert result["rolled_back"] is True


async def test_failed_rollback_leaves_the_migration_applied(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    """A reversal that fails mid-way rolls back the whole transaction — the original
    migration stays applied, which is the only state that remains describable."""
    target: dict[str, Any] = {"tables": {"items": {"columns": {"y_col": {"type": "text"}}}}}
    await _apply_migration(conn_manager, config, audit, tokens, target, name="add-y")
    ledger_id = await _latest_ledger_id(conn_manager)

    with pytest.raises(PgopsError) as exc_info:
        await rollback_migration(conn_manager, audit, tokens, ledger_id)
    token = await _token_from(exc_info.value)

    # Sabotage: rename the table behind the ledger's back. The recorded inverse
    # (`ALTER TABLE "items" ...`) now references a nonexistent relation and must fail.
    # (Renaming only the column would NOT work: the inverse says DROP COLUMN IF EXISTS,
    # which correctly no-ops on a missing column.)
    pool = await conn_manager.readwrite_pool()
    async with pool.acquire() as conn:
        await conn.execute("ALTER TABLE items RENAME TO items_moved")

    with pytest.raises(PgopsError) as exc_fail:
        await rollback_migration(conn_manager, audit, tokens, ledger_id, confirm_token=token)
    assert exc_fail.value.code is ErrorCode.MIGRATION_FAILED

    # the migration is still applied under its own identity
    async with pool.acquire() as conn:
        status = await conn.fetchval(f"SELECT status FROM {LEDGER_TABLE} WHERE id = $1", ledger_id)
    assert status == "applied"
    # restore the fixture for subsequent tests
    async with pool.acquire() as conn:
        await conn.execute("ALTER TABLE items_moved RENAME TO items")
    assert "renamed" in await _columns(config.dsn, "items") or "y_col" in await _columns(
        config.dsn, "items"
    )


async def test_rollback_records_verdicts_in_the_audit_log(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    log = AuditLog(config.audit_path)
    target: dict[str, Any] = {"tables": {"items": {"columns": {"z_col": {"type": "text"}}}}}
    await _apply_migration(conn_manager, config, log, tokens, target)
    ledger_id = await _latest_ledger_id(conn_manager)

    with pytest.raises(PgopsError) as exc_info:
        await rollback_migration(conn_manager, log, tokens, ledger_id)
    token = await _token_from(exc_info.value)
    await rollback_migration(conn_manager, log, tokens, ledger_id, confirm_token=token)

    verdicts = [e["verdict"] for e in log.read_all()]
    assert verdicts.count("refused_pending_confirmation") == 1
    assert verdicts[-1] == "executed"
