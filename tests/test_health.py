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


async def test_connections_finding_excludes_background_workers(
    conn_manager: ConnectionManager,
) -> None:
    """pg_stat_activity is mostly not connections.

    A stock Postgres 16 runs a checkpointer, walwriter, background writer, autovacuum
    launcher and logical replication launcher. All five report a NULL state and none
    occupies a max_connections slot, but an unfiltered count reported them as
    "5 active backend connection(s)" bucketed under "unknown" — on a database with one
    client attached. Only `backend_type = 'client backend'` belongs in this number.
    """
    report = await db_health(conn_manager)
    conns = next(f for f in report.findings if f.category == "connections")
    assert "unknown" not in conns.detail["by_state"], (
        f"a NULL-state backend was counted as a connection: {conns.detail['by_state']}"
    )
    # This test holds one itself, so the floor is 1 — and pgops's own backend must be
    # counted, since the whole point of the finding is max_connections headroom.
    assert conns.detail["total"] >= 1
    assert conns.detail["max_connections"] > 0
    assert f"of {conns.detail['max_connections']} max" in conns.summary


async def test_bloat_is_not_reported_for_a_freshly_loaded_table(
    conn_manager: ConnectionManager, dsn: str
) -> None:
    """A table that has never been updated or deleted from has no bloat to reclaim.

    The estimator used to compare actual size against `reltuples × sum(avg_width)`,
    which omits the 24-byte MAXALIGNed tuple header, the 4-byte line pointer and the
    24-byte page header. On narrow rows that understates live bytes by roughly 45% and
    reports the shortfall as reclaimable — measured on a real 600k-row table as
    "critical: roughly 55% bloat" when pgstattuple put dead+free at 0.05%. The remedy an
    agent reaches for on a critical bloat finding is VACUUM FULL, which takes an
    AccessExclusiveLock and rewrites the table.
    """
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("DROP TABLE IF EXISTS bloat_probe")
        # Narrow rows are where the old formula was worst: the fixed per-tuple overhead
        # is the majority of the row.
        await conn.execute(
            "CREATE TABLE bloat_probe AS "
            "SELECT g AS id, (g % 97) AS k FROM generate_series(1, 60000) g"
        )
        await conn.execute("ANALYZE bloat_probe")
        report = await db_health(conn_manager)
        bloat = [
            f
            for f in report.findings
            if f.category == "bloat" and f.detail.get("table") == "bloat_probe"
        ]
        assert not bloat, f"clean table reported as bloated: {[f.summary for f in bloat]}"
    finally:
        await conn.execute("DROP TABLE IF EXISTS bloat_probe")
        await conn.close()


async def test_bloat_is_still_reported_when_it_is_real(
    conn_manager: ConnectionManager, dsn: str
) -> None:
    """The fix must not silence the finding — only stop it firing on healthy tables.

    Churning every row twice and deleting half, with autovacuum off, leaves a table that
    pgstattuple measures at ~80% dead+free. The corrected estimator puts it at ~84%,
    which is the accuracy that makes the finding worth acting on.
    """
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("DROP TABLE IF EXISTS bloat_real")
        await conn.execute(
            "CREATE TABLE bloat_real AS "
            "SELECT g AS id, repeat('x', 80) AS pad FROM generate_series(1, 40000) g"
        )
        await conn.execute("ALTER TABLE bloat_real SET (autovacuum_enabled = false)")
        await conn.execute("UPDATE bloat_real SET pad = repeat('y', 80)")
        await conn.execute("UPDATE bloat_real SET pad = repeat('z', 80)")
        await conn.execute("DELETE FROM bloat_real WHERE id % 2 = 0")
        await conn.execute("ANALYZE bloat_real")
        report = await db_health(conn_manager)
        bloat = [
            f
            for f in report.findings
            if f.category == "bloat" and f.detail.get("table") == "bloat_real"
        ]
        assert bloat, "a table that is genuinely ~80% dead space was not flagged"
        assert bloat[0].detail["estimated_waste_pct"] > 50
    finally:
        await conn.execute("DROP TABLE IF EXISTS bloat_real")
        await conn.close()
