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
    with PostgresContainer(
        "postgres:16", username="pgops_test", password="pgops_test", dbname="pgops_test"
    ) as container:
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
        # DROP + CREATE, not CREATE IF NOT EXISTS + TRUNCATE. Once migration tests exist,
        # a test can legitimately ALTER or drop a column of `items`; a fixture that only
        # resets *rows* would then hand the next test a table with the wrong *shape*
        # (seeding failed with `column "name" of relation "items" does not exist`).
        # The fixture has to guarantee the schema, not just the data.
        await setup_conn.execute("DROP TABLE IF EXISTS items")
        await setup_conn.execute(
            """
            CREATE TABLE items (
                id serial PRIMARY KEY,
                name text NOT NULL
            )
            """
        )
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


@pytest_asyncio.fixture(scope="session")
async def perf_dsn(dsn: str) -> str:
    """Seeds tables big enough for plan analysis, once per session.

    Phase 3 rules key off absolute row counts (a sequential scan is only a problem on a
    large table), so these scenarios need real volume — a 250-row fixture would produce
    a plan where every verdict is correctly silent, proving nothing. 60k rows is enough
    to trigger seq-scan and sort-spill thresholds while still seeding in ~1s.

    Session-scoped and read-only from tests: the perf tables are never mutated, so no
    per-test reset is needed and the seeding cost is paid once.
    """
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("DROP TABLE IF EXISTS perf_events, perf_users")
        await conn.execute(
            """
            CREATE TABLE perf_users (
                id serial PRIMARY KEY,
                email text NOT NULL,
                region text NOT NULL
            )
            """
        )
        # Deliberately NO index on perf_events.status or .created_at — the missing
        # index IS the scenario. Only user_id is indexed, mirroring dev/init.sql.
        await conn.execute(
            """
            CREATE TABLE perf_events (
                id serial PRIMARY KEY,
                user_id integer NOT NULL,
                status text NOT NULL,
                payload text NOT NULL,
                created_at timestamptz NOT NULL
            )
            """
        )
        await conn.execute(
            """
            INSERT INTO perf_users (email, region)
            SELECT 'user' || i || '@example.com',
                   (ARRAY['us','eu','apac'])[1 + (i % 3)]
            FROM generate_series(1, 5000) AS i
            """
        )
        await conn.execute(
            """
            INSERT INTO perf_events (user_id, status, payload, created_at)
            SELECT 1 + (i % 5000),
                   CASE WHEN i % 10000 = 0 THEN 'rare' ELSE 'common' END,
                   repeat('x', 100),
                   now() - ((60000 - i) || ' seconds')::interval
            FROM generate_series(1, 60000) AS i
            """
        )
        await conn.execute("CREATE INDEX idx_perf_events_user_id ON perf_events (user_id)")
        await conn.execute("ANALYZE perf_users")
        await conn.execute("ANALYZE perf_events")
    finally:
        await conn.close()
    return dsn


def free_port() -> int:
    """Reserve an unused TCP port for a test that boots a real HTTP server.

    Fixed port numbers were used here originally, and they caused exactly the failure
    they always cause: two suites (`test_live_server` and `test_audit_identity`) both
    picked 8795, so running the full suite meant one of them either failed to bind or —
    worse — connected to the *other* suite's server and made assertions about it. The
    symptom was a cascade of unrelated errors in whichever suite ran second, and it
    passed in isolation, which is the signature of a shared-resource collision rather
    than a real defect.

    Function-scoped fixtures make it worse: a suite that boots a server per test rebinds
    the same port fifteen times in a row, which on Windows can land in TIME_WAIT.

    Binding port 0 lets the OS pick something free. There is a small race between
    closing this socket and uvicorn binding it, but it is far narrower than the
    certainty of collision with hardcoded numbers.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
