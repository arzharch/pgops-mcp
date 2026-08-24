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
