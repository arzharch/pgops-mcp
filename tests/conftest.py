"""Testcontainers Postgres fixtures (ADR-005: no mocks for anything DB-facing).

Session-scoped container, function-scoped ConnectionManager/schema so tests don't
interfere with each other's rows while still paying the container startup cost once.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from testcontainers.community.postgres import PostgresContainer

from pgops.audit import AuditLog
from pgops.config import PgopsConfig
from pgops.connections import ConnectionManager
from pgops.guardrails import ConfirmationTokenStore


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    with PostgresContainer("postgres:16", username="pgops_test", password="pgops_test", dbname="pgops_test") as container:
        yield container


@pytest.fixture(scope="session")
def dsn(postgres_container: PostgresContainer) -> str:
    host = postgres_container.get_container_host_ip()
    port = postgres_container.get_exposed_port(5432)
    return f"postgresql://pgops_test:pgops_test@{host}:{port}/pgops_test"


@pytest.fixture
def config(dsn: str, tmp_path: Path) -> PgopsConfig:
    # audit log into tmp_path so tests never touch the developer's real ~/.pgops log
    return PgopsConfig.from_env(dsn=dsn, audit_path=tmp_path / "audit.jsonl")


@pytest_asyncio.fixture
async def conn_manager(config: PgopsConfig) -> AsyncIterator[ConnectionManager]:
    # Seed with a raw, non-pooled connection — NOT through ConnectionManager.readonly_pool,
    # which forces `default_transaction_read_only = on` and would refuse this DDL/INSERT
    # itself (that refusal is exactly what test_connections.py proves).
    setup_conn = await asyncpg.connect(config.dsn)
    try:
        await setup_conn.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
                id serial PRIMARY KEY,
                name text NOT NULL
            )
            """
        )
        # RESTART IDENTITY matters: plain TRUNCATE leaves the serial sequence where it
        # was, so `items` would be re-seeded with ids continuing from the previous
        # test's high-water mark. Any assertion phrased as `WHERE id <= 5` would then
        # match a different number of rows depending on which tests ran before it —
        # test outcomes that depend on execution order are worse than no tests.
        await setup_conn.execute("TRUNCATE items RESTART IDENTITY")
        await setup_conn.executemany(
            "INSERT INTO items (name) VALUES ($1)",
            [(f"item-{i}",) for i in range(250)],
        )
    finally:
        await setup_conn.close()

    manager = ConnectionManager(config)
    await manager.start()
    try:
        yield manager
    finally:
        await manager.stop()


@pytest.fixture
def audit(config: PgopsConfig) -> AuditLog:
    return AuditLog(config.audit_path)


@pytest.fixture
def tokens() -> ConfirmationTokenStore:
    return ConfirmationTokenStore()
