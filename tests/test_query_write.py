"""query.write against real Postgres: the full refuse → confirm → execute → audit path.

ADR-005: guardrails are proven against the real engine, never mocks. A guardrail that
"blocks" a DELETE in a mock proves only that the mock was not asked to delete anything.
Here the assertions are on actual row counts after the call.
"""

from __future__ import annotations

import asyncpg
import pytest

from pgops.audit import AuditLog
from pgops.config import PgopsConfig
from pgops.connections import ConnectionManager
from pgops.errors import ErrorCode, PgopsError
from pgops.guardrails import ConfirmationTokenStore
from pgops.tools.write import query_write


async def _count(dsn: str) -> int:
    conn = await asyncpg.connect(dsn)
    try:
        return int(await conn.fetchval("SELECT count(*) FROM items"))
    finally:
        await conn.close()


async def test_bounded_delete_executes(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    result = await query_write(
        conn_manager, config, audit, tokens, "DELETE FROM items WHERE id <= 10"
    )
    assert result.rows_affected == 10
    assert await _count(config.dsn) == 240


async def test_insert_executes(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    result = await query_write(
        conn_manager, config, audit, tokens, "INSERT INTO items (name) VALUES ('new')"
    )
    assert result.rows_affected == 1
    assert await _count(config.dsn) == 251


async def test_unbounded_delete_is_refused_and_changes_nothing(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    with pytest.raises(PgopsError) as exc_info:
        await query_write(conn_manager, config, audit, tokens, "DELETE FROM items")
    assert exc_info.value.code is ErrorCode.CONFIRMATION_REQUIRED
    # the actual guarantee: the rows are still there
    assert await _count(config.dsn) == 250


async def test_confirm_flow_executes_after_token(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    sql = "DELETE FROM items"
    with pytest.raises(PgopsError) as exc_info:
        await query_write(conn_manager, config, audit, tokens, sql)

    # the agent parses the token out of the refusal hint and calls again
    hint = exc_info.value.hint or ""
    token = hint.split("confirm_token=")[1].split("'")[1]

    result = await query_write(conn_manager, config, audit, tokens, sql, confirm_token=token)
    assert result.rows_affected == 250
    assert await _count(config.dsn) == 0


async def test_token_from_one_statement_cannot_run_another(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    """End-to-end version of the token-binding attack: approval obtained for a narrow
    statement must not execute a broader one."""
    token = tokens.issue("DELETE FROM items WHERE name = 'nonexistent'", "test")
    with pytest.raises(PgopsError) as exc_info:
        await query_write(
            conn_manager, config, audit, tokens, "DELETE FROM items", confirm_token=token
        )
    assert exc_info.value.code is ErrorCode.CONFIRMATION_MISMATCH
    assert await _count(config.dsn) == 250


async def test_read_statement_routed_away(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    with pytest.raises(PgopsError) as exc_info:
        await query_write(conn_manager, config, audit, tokens, "SELECT * FROM items")
    assert exc_info.value.code is ErrorCode.INVALID_ARGUMENT


async def test_read_only_mode_disables_writes(
    dsn: str, audit: AuditLog, tokens: ConfirmationTokenStore, tmp_path: object
) -> None:
    """--read-only must fail before a write-capable connection is ever opened."""
    ro_config = PgopsConfig.from_env(dsn=dsn, read_only=True)
    manager = ConnectionManager(ro_config)
    await manager.start()
    try:
        with pytest.raises(PgopsError) as exc_info:
            await query_write(
                manager, ro_config, audit, tokens, "INSERT INTO items (name) VALUES ('x')"
            )
        assert exc_info.value.code is ErrorCode.READ_ONLY_MODE
    finally:
        await manager.stop()


async def test_failed_statement_rolls_back(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    """A statement that violates a constraint must leave no partial effect."""
    with pytest.raises(PgopsError) as exc_info:
        await query_write(
            conn_manager, config, audit, tokens, "INSERT INTO items (name) VALUES (NULL)"
        )
    assert exc_info.value.code is ErrorCode.INVALID_ARGUMENT
    assert await _count(config.dsn) == 250
