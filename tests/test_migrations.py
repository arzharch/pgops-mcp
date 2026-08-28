"""Migration engine against real Postgres (SPEC Phase 4 gate, ADR-005).

Transactional DDL, crash recovery and lock behaviour are all properties of the engine,
not of our code — asserting them against a mock would prove nothing at all.
"""

from __future__ import annotations

import re
from typing import Any

import asyncpg
import pytest

from pgops.audit import AuditLog
from pgops.config import PgopsConfig
from pgops.connections import ConnectionManager
from pgops.errors import ErrorCode, PgopsError
from pgops.guardrails import ConfirmationTokenStore
from pgops.migrations.ledger import LEDGER_TABLE, MigrationLedger, checksum_steps
from pgops.tools.migrations import (
    migration_apply,
    migration_history,
    migration_plan,
    migration_resolve,
)


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


async def _token_from(exc: PgopsError) -> str:
    return (exc.hint or "").split("confirm_token=")[1].split("'")[1]


# --- planning -----------------------------------------------------------------------


async def test_plan_produces_annotated_steps(
    conn_manager: ConnectionManager, config: PgopsConfig
) -> None:
    target: dict[str, Any] = {"tables": {"items": {"columns": {"note": {"type": "text"}}}}}
    plan = await migration_plan(conn_manager, config, target)
    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.change.sql == 'ALTER TABLE "items" ADD COLUMN "note" text'
    assert step.impact.operation.value == "metadata_only"
    assert step.impact.reasoning
    assert plan.dry_run_ok is True


async def test_plan_executes_nothing(conn_manager: ConnectionManager, config: PgopsConfig) -> None:
    """The dry run runs the DDL for real inside a transaction — the rollback must be
    total, or `plan` silently becomes `apply`."""
    target: dict[str, Any] = {"tables": {"items": {"columns": {"ghost": {"type": "text"}}}}}
    await migration_plan(conn_manager, config, target)
    assert "ghost" not in await _columns(config.dsn, "items")


async def test_plan_is_idempotent_when_schema_matches(
    conn_manager: ConnectionManager, config: PgopsConfig
) -> None:
    target: dict[str, Any] = {"tables": {"items": {"columns": {"name": {"type": "text"}}}}}
    plan = await migration_plan(conn_manager, config, target)
    assert plan.steps == []
    assert any("already matches" in n for n in plan.notes)


async def test_dry_run_catches_a_statement_that_would_fail(
    conn_manager: ConnectionManager, config: PgopsConfig
) -> None:
    """Static analysis cannot know a type doesn't exist; executing it in a doomed
    transaction can. This is the failure that would otherwise appear halfway through a
    real apply with earlier steps already committed."""
    target: dict[str, Any] = {"tables": {"items": {"columns": {"bad": {"type": "no_such_type"}}}}}
    plan = await migration_plan(conn_manager, config, target)
    assert plan.dry_run_ok is False
    assert "would fail" in (plan.steps[0].dry_run or "")


async def test_concurrent_index_makes_plan_non_atomic(
    conn_manager: ConnectionManager, config: PgopsConfig
) -> None:
    """CREATE INDEX CONCURRENTLY cannot run in a transaction (verified against PG16),
    so a plan containing one cannot promise all-or-nothing. Saying so is the point."""
    target: dict[str, Any] = {
        "tables": {"items": {"columns": {"name": {"type": "text"}}, "indexes": {"idx_n": "name"}}}
    }
    plan = await migration_plan(conn_manager, config, target)
    # plain CREATE INDEX is transactional, so this plan IS atomic
    assert plan.atomic is True

    from pgops.migrations.lock_analysis import analyze_statement

    impact = analyze_statement("CREATE INDEX CONCURRENTLY i ON items (name)", 1000)
    assert impact.transactional is False


# --- applying -----------------------------------------------------------------------


async def test_apply_executes_and_records_in_ledger(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    target: dict[str, Any] = {"tables": {"items": {"columns": {"note": {"type": "text"}}}}}
    plan = await migration_plan(conn_manager, config, target)
    result = await migration_apply(
        conn_manager, config, audit, tokens, plan.plan_id, name="add-note"
    )
    assert result["applied"] is True
    assert "note" in await _columns(config.dsn, "items")

    history = await migration_history(conn_manager)
    entry = history["history"][0]
    assert entry["status"] == "applied"
    assert entry["name"] == "add-note"
    assert entry["checksum"] == checksum_steps(plan.sql_steps)


async def test_reapplying_the_same_plan_is_a_noop(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    target: dict[str, Any] = {"tables": {"items": {"columns": {"note2": {"type": "text"}}}}}
    plan = await migration_plan(conn_manager, config, target)
    await migration_apply(conn_manager, config, audit, tokens, plan.plan_id)
    again = await migration_apply(conn_manager, config, audit, tokens, plan.plan_id)
    assert again["applied"] is False
    assert again["reason"] == "already applied"


async def test_unknown_plan_id_refused(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    with pytest.raises(PgopsError) as exc_info:
        await migration_apply(conn_manager, config, audit, tokens, "nope")
    assert exc_info.value.code is ErrorCode.INVALID_ARGUMENT


async def test_destructive_migration_requires_confirmation(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    target: dict[str, Any] = {"tables": {"items": {"columns": {"id": {"type": "integer"}}}}}
    plan = await migration_plan(conn_manager, config, target, allow_drops=True)
    assert any(s.change.destructive for s in plan.steps)

    with pytest.raises(PgopsError) as exc_info:
        await migration_apply(conn_manager, config, audit, tokens, plan.plan_id)
    assert exc_info.value.code is ErrorCode.CONFIRMATION_REQUIRED
    # nothing dropped
    assert "name" in await _columns(config.dsn, "items")


async def test_destructive_migration_executes_with_token(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    target: dict[str, Any] = {"tables": {"items": {"columns": {"id": {"type": "integer"}}}}}
    plan = await migration_plan(conn_manager, config, target, allow_drops=True)
    with pytest.raises(PgopsError) as exc_info:
        await migration_apply(conn_manager, config, audit, tokens, plan.plan_id)
    token = await _token_from(exc_info.value)

    result = await migration_apply(
        conn_manager, config, audit, tokens, plan.plan_id, confirm_token=token
    )
    assert result["applied"] is True
    assert "name" not in await _columns(config.dsn, "items")


async def test_confirmation_reason_names_the_data_loss(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    """The agent relays this to a human, so it has to say what is actually lost."""
    target: dict[str, Any] = {"tables": {"items": {"columns": {"id": {"type": "integer"}}}}}
    plan = await migration_plan(conn_manager, config, target, allow_drops=True)
    with pytest.raises(PgopsError) as exc_info:
        await migration_apply(conn_manager, config, audit, tokens, plan.plan_id)
    assert "items.name" in exc_info.value.message


async def test_migration_token_bound_to_the_plan(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    """Approval for one migration must not apply a different one.

    Both plans below are destructive but drop *different* columns — the realistic shape
    of the attack, where a user approves a change they were shown and a different one
    gets executed with that approval.
    """
    target_a: dict[str, Any] = {"tables": {"items": {"columns": {"id": {"type": "integer"}}}}}
    plan_a = await migration_plan(conn_manager, config, target_a, allow_drops=True)
    with pytest.raises(PgopsError) as exc_info:
        await migration_apply(conn_manager, config, audit, tokens, plan_a.plan_id)
    token_a = await _token_from(exc_info.value)
    assert plan_a.steps and all(s.change.target == "name" for s in plan_a.steps)

    target_b: dict[str, Any] = {"tables": {"items": {"columns": {"name": {"type": "text"}}}}}
    plan_b = await migration_plan(conn_manager, config, target_b, allow_drops=True)
    assert plan_b.checksum != plan_a.checksum
    with pytest.raises(PgopsError) as exc_info_b:
        await migration_apply(
            conn_manager, config, audit, tokens, plan_b.plan_id, confirm_token=token_a
        )
    assert exc_info_b.value.code is ErrorCode.CONFIRMATION_MISMATCH
    # and nothing was dropped by the rejected attempt
    assert {"id", "name"} <= await _columns(config.dsn, "items")


# --- failure and crash recovery -----------------------------------------------------


async def test_failed_migration_rolls_back_every_step(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    """Postgres has transactional DDL — a migration that fails on step 3 must not leave
    steps 1 and 2 applied. This is the property that makes an atomic claim true."""
    target: dict[str, Any] = {
        "tables": {
            "items": {
                "columns": {
                    "good_one": {"type": "text"},
                    "good_two": {"type": "integer"},
                    "bad": {"type": "no_such_type"},
                }
            }
        }
    }
    plan = await migration_plan(conn_manager, config, target, dry_run=False)
    with pytest.raises(PgopsError) as exc_info:
        await migration_apply(conn_manager, config, audit, tokens, plan.plan_id)
    assert exc_info.value.code is ErrorCode.MIGRATION_FAILED

    columns = await _columns(config.dsn, "items")
    assert "good_one" not in columns
    assert "good_two" not in columns


async def test_failed_migration_is_recorded_as_failed(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    target: dict[str, Any] = {"tables": {"items": {"columns": {"bad": {"type": "no_such_type"}}}}}
    plan = await migration_plan(conn_manager, config, target, dry_run=False)
    with pytest.raises(PgopsError):
        await migration_apply(conn_manager, config, audit, tokens, plan.plan_id)

    history = await migration_history(conn_manager)
    assert history["history"][0]["status"] == "failed"
    assert history["history"][0]["error"]
    # a failed migration must not block the next one
    assert history["in_flight"] == []


async def test_interrupted_migration_blocks_further_applies(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    """Simulates a process killed mid-migration: a row left `in_flight`.

    The tool must refuse to continue rather than guess which steps landed — the whole
    reason the ledger row is written BEFORE the DDL rather than after.
    """
    pool = await conn_manager.readwrite_pool()
    async with pool.acquire() as conn:
        ledger = MigrationLedger(conn)
        await ledger.ensure_table()
        await ledger.begin("crashed-one", "interrupted", "deadbeef", ["ALTER ..."], "test")

    target: dict[str, Any] = {"tables": {"items": {"columns": {"after_crash": {"type": "text"}}}}}
    plan = await migration_plan(conn_manager, config, target)
    with pytest.raises(PgopsError) as exc_info:
        await migration_apply(conn_manager, config, audit, tokens, plan.plan_id)
    assert exc_info.value.code is ErrorCode.MIGRATION_IN_FLIGHT
    assert "crashed-one" in exc_info.value.message
    assert "after_crash" not in await _columns(config.dsn, "items")

    async with pool.acquire() as conn:
        await conn.execute(f"DELETE FROM {LEDGER_TABLE} WHERE migration_id = 'crashed-one'")


async def test_history_surfaces_in_flight_warning(
    conn_manager: ConnectionManager, config: PgopsConfig
) -> None:
    pool = await conn_manager.readwrite_pool()
    async with pool.acquire() as conn:
        ledger = MigrationLedger(conn)
        await ledger.ensure_table()
        await ledger.begin("stuck", "stuck", "abc", [], "test")

    history = await migration_history(conn_manager)
    assert history["warning"] is not None
    assert history["in_flight"][0]["migration_id"] == "stuck"

    async with pool.acquire() as conn:
        await conn.execute(f"DELETE FROM {LEDGER_TABLE} WHERE migration_id = 'stuck'")


# --- ledger internals ---------------------------------------------------------------


def test_checksum_is_order_sensitive() -> None:
    """The same statements in a different order can produce a different schema, so a
    set-based hash would call two genuinely different migrations identical."""
    assert checksum_steps(["A", "B"]) != checksum_steps(["B", "A"])


def test_checksum_separator_prevents_collisions() -> None:
    assert checksum_steps(["ab", "c"]) != checksum_steps(["a", "bc"])


async def test_ledger_allows_retry_after_failure_but_not_double_apply(
    conn_manager: ConnectionManager,
) -> None:
    """Partial unique index: a migration may appear twice if the first attempt failed,
    but must never be `applied` twice."""
    pool = await conn_manager.readwrite_pool()
    async with pool.acquire() as conn:
        ledger = MigrationLedger(conn)
        await ledger.ensure_table()
        first = await ledger.begin("retry-me", "n", "sum", [], "test")
        await ledger.fail(first, "boom")
        second = await ledger.begin("retry-me", "n", "sum", [], "test")
        await ledger.finish(second, 1.0)

        with pytest.raises(asyncpg.UniqueViolationError):
            third = await ledger.begin("retry-me", "n", "sum", [], "test")
            await ledger.finish(third, 1.0)

        await conn.execute(f"DELETE FROM {LEDGER_TABLE} WHERE migration_id = 'retry-me'")


def test_asyncpg_interface_error_is_not_a_postgres_error() -> None:
    """The root cause of the interrupted-migration bug, stated as an assertion.

    `_execute_plan` caught `asyncpg.PostgresError`, which covers errors Postgres *sends*.
    A lost connection is raised by the driver instead, and `InterfaceError` shares no
    ancestor with `PostgresError` — so the handler never saw it and the caller got
    `INTERNAL_ERROR: internal error; see server logs`.
    """
    assert not issubclass(asyncpg.InterfaceError, asyncpg.PostgresError)


async def test_connection_lost_mid_apply_tells_the_caller_how_to_recover(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A migration interrupted by connection loss must return a usable error.

    Reproduced against a live stack by SIGKILLing Postgres mid-index-build: the ledger
    correctly recorded `in_flight` and every later apply refused, but the agent holding
    the failure was told only "internal error; see server logs" — not that a migration
    was half-applied, not that further applies would refuse, not that migration.resolve
    is how it ends. Under stdio the human usually cannot read that log either.

    The exception is injected rather than provoked so the test is deterministic; it is
    the exact type and message asyncpg raises when the socket dies under an open
    transaction.
    """
    plan = await migration_plan(
        conn_manager, config, {"tables": {"items": {"columns": {"lost": {"type": "text"}}}}}
    )
    real_execute = asyncpg.Connection.execute

    async def die_on_the_migration(self: Any, query: str, *args: Any, **kwargs: Any) -> Any:
        if "lost" in query and "ALTER TABLE" in query:
            raise asyncpg.InterfaceError(
                "cannot call Transaction.__aexit__(): the underlying connection is closed"
            )
        return await real_execute(self, query, *args, **kwargs)

    monkeypatch.setattr(asyncpg.Connection, "execute", die_on_the_migration)
    with pytest.raises(PgopsError) as exc:
        await migration_apply(conn_manager, config, audit, tokens, plan.plan_id, name="lossy")
    monkeypatch.undo()

    assert exc.value.code is ErrorCode.MIGRATION_INTERRUPTED
    assert "connection was lost" in exc.value.message
    hint = exc.value.hint or ""
    assert "migration.resolve" in hint, "the error must name the way out"
    assert "in_flight" in hint

    # The row it points at must actually be there, and the id in the hint must be real.
    ledger_id = int(re.search(r"ledger_id=(\d+)", hint).group(1))  # type: ignore[union-attr]
    history = await migration_history(conn_manager, limit=10)
    stranded = {e["ledger_id"]: e for e in history["in_flight"]}
    assert ledger_id in stranded, f"hint names ledger_id={ledger_id}, in_flight={list(stranded)}"
    assert stranded[ledger_id]["status"] == "in_flight"

    # And it is resolvable, so the tool is not left wedged.
    with pytest.raises(PgopsError) as needs_token:
        await migration_resolve(conn_manager, audit, tokens, ledger_id, "failed", "column absent")
    token = (needs_token.value.hint or "").split("confirm_token=")[1].strip("'")
    result = await migration_resolve(
        conn_manager, audit, tokens, ledger_id, "failed", "column absent", confirm_token=token
    )
    assert result["resolved"] is True
