"""db.health against a real database."""

from __future__ import annotations

import json

import asyncpg

from pgops.connections import ConnectionManager
from pgops.tools.health import db_health


async def test_health_reports_core_categories(conn_manager: ConnectionManager) -> None:
    report = await db_health(conn_manager)
    categories = {f.category for f in report.findings}
    assert "connections" in categories
    assert "cache_hit_ratio" in categories


async def test_health_findings_are_json_serializable(
    conn_manager: ConnectionManager, dsn: str
) -> None:
    """Every finding must survive JSON encoding — MCP results are JSON, so a stray
    Decimal or timedelta is a transport-layer failure, not a cosmetic one.

    Dead tuples are *generated* here rather than hoped for: on a freshly seeded
    container that finding is absent, so an assertion over the default state silently
    skips the branch most likely to contain an unencodable type (dead_pct is a Postgres
    numeric → Decimal). This test failed to catch exactly that until it made the rows.
    """
    churn = await asyncpg.connect(dsn)
    try:
        await churn.execute("UPDATE items SET name = name || '-x'")
    finally:
        await churn.close()

    report = await db_health(conn_manager)
    categories = {f.category for f in report.findings}
    assert "dead_tuples" in categories, "expected UPDATE churn to produce dead tuples"
    json.dumps(report.to_dict())


async def test_health_severities_are_known_values(conn_manager: ConnectionManager) -> None:
    report = await db_health(conn_manager)
    assert all(f.severity in {"ok", "info", "warning", "critical"} for f in report.findings)


async def test_idle_database_has_no_lock_contention(conn_manager: ConnectionManager) -> None:
    report = await db_health(conn_manager)
    categories = {f.category for f in report.findings}
    # nothing is blocking or running long on an idle test container. Deliberately not
    # asserting on dead_tuples here — whether that finding appears depends on churn from
    # whichever tests ran before, and coupling an assertion to test order would make
    # this fail for reasons that have nothing to do with health reporting.
    assert "waiting_locks" not in categories
    assert "long_running_queries" not in categories
