"""Proves the readonly pool actually can't write — at the Postgres level, not just
the classifier level (ADR-001 defense-in-depth: connections.py docstring)."""

from __future__ import annotations

import asyncpg
import pytest

from pgops.connections import ConnectionManager


async def test_readonly_pool_rejects_write_even_with_full_privilege_dsn(
    conn_manager: ConnectionManager,
) -> None:
    # PGOPS_DSN in this test suite is the container's superuser role — if anything
    # were going to slip a write through on privilege alone, it'd be this DSN.
    with pytest.raises(asyncpg.PostgresError) as exc_info:
        async with conn_manager.readonly_pool.acquire() as conn:
            await conn.execute("INSERT INTO items (name) VALUES ('should-not-work')")
    assert "read-only" in str(exc_info.value).lower()


async def test_readonly_pool_allows_reads(conn_manager: ConnectionManager) -> None:
    async with conn_manager.readonly_pool.acquire() as conn:
        count = await conn.fetchval("SELECT count(*) FROM items")
    assert count == 250


async def test_healthcheck_reports_readonly_ok(conn_manager: ConnectionManager) -> None:
    result = await conn_manager.healthcheck()
    assert result["readonly"] is True
    assert result["readwrite"] is False  # never opened in this test


@pytest.mark.parametrize(
    "sql, label",
    [
        ("SELECT id FROM items LIMIT 1 FOR UPDATE", "row lock"),
        ("SELECT nextval('items_id_seq')", "sequence advance"),
        ("CREATE TABLE sneaky (id int)", "DDL"),
        ("TRUNCATE items", "truncate"),
    ],
)
async def test_readonly_pool_blocks_non_obvious_write_vectors(
    conn_manager: ConnectionManager, sql: str, label: str
) -> None:
    """The classifier's job is to catch these *before* execution — but these are the
    statements that would do damage if it ever failed to.

    `SELECT ... FOR UPDATE` and `nextval()` are the interesting two: both are
    lexically SELECTs, so a keyword-based classifier alone would happily wave them
    through, and both have real side effects (row locks that block the application;
    sequence state that cannot be rolled back). Postgres refuses both in a read-only
    transaction, which is what makes the pool-level guarantee — not the classifier —
    the actual last line of defense.
    """
    with pytest.raises(asyncpg.PostgresError) as exc_info:
        async with conn_manager.acquire_readonly() as conn:
            await conn.execute(sql)
    assert "read-only transaction" in str(exc_info.value), label


async def test_acquire_readonly_returns_connection_to_pool(
    conn_manager: ConnectionManager,
) -> None:
    """Pool exhaustion is only survivable if connections come back on the error path
    too — an early `raise` inside the context manager must still release."""
    for _ in range(20):  # far more iterations than the pool's max size
        try:
            async with conn_manager.acquire_readonly() as conn:
                await conn.fetchval("SELECT 1")
                raise RuntimeError("simulated tool failure")
        except RuntimeError:
            pass
    async with conn_manager.acquire_readonly() as conn:
        assert await conn.fetchval("SELECT 1") == 1
