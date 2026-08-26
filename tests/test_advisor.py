"""index.advise against real Postgres."""

from __future__ import annotations

import json

import asyncpg

from pgops.connections import ConnectionManager
from pgops.tools.advisor import index_advise


async def test_detects_unused_index(
    perf_dsn: str, dsn: str, conn_manager: ConnectionManager
) -> None:
    setup = await asyncpg.connect(dsn)
    try:
        await setup.execute("DROP INDEX IF EXISTS idx_never_used")
        await setup.execute("CREATE INDEX idx_never_used ON perf_events (payload)")
    finally:
        await setup.close()

    advice = await index_advise(conn_manager)
    names = {i["index_name"] for i in advice.unused_indexes}
    assert "idx_never_used" in names
    finding = next(i for i in advice.unused_indexes if i["index_name"] == "idx_never_used")
    # A fresh test container has minutes of statistics, not weeks — the finding is
    # reported but must NOT come with a confident DROP recommendation.
    assert finding["confidence"] == "low"
    assert "do NOT drop" in finding["suggestion"]
    assert advice.stats_window["sufficient_for_unused_index_advice"] is False

    cleanup = await asyncpg.connect(dsn)
    try:
        await cleanup.execute("DROP INDEX IF EXISTS idx_never_used")
    finally:
        await cleanup.close()


async def test_primary_key_never_reported_as_unused(
    perf_dsn: str, conn_manager: ConnectionManager
) -> None:
    """A PK exists to enforce correctness. "Nobody scanned it" is not a reason to drop
    it, and advising that would be actively harmful."""
    advice = await index_advise(conn_manager)
    names = {i["index_name"] for i in advice.unused_indexes}
    assert "perf_events_pkey" not in names
    assert "perf_users_pkey" not in names


async def test_detects_redundant_prefix_index(
    perf_dsn: str, dsn: str, conn_manager: ConnectionManager
) -> None:
    setup = await asyncpg.connect(dsn)
    try:
        await setup.execute("DROP INDEX IF EXISTS idx_prefix, idx_composite")
        await setup.execute("CREATE INDEX idx_prefix ON perf_events (status)")
        await setup.execute("CREATE INDEX idx_composite ON perf_events (status, created_at)")
    finally:
        await setup.close()

    advice = await index_advise(conn_manager)
    redundant = {r["index"]: r for r in advice.redundant_indexes}
    assert "idx_prefix" in redundant
    assert redundant["idx_prefix"]["superseded_by"] == "idx_composite"
    # the composite is NOT redundant — it is the one doing the work
    assert "idx_composite" not in redundant

    cleanup = await asyncpg.connect(dsn)
    try:
        await cleanup.execute("DROP INDEX IF EXISTS idx_prefix, idx_composite")
    finally:
        await cleanup.close()


async def test_unique_index_not_reported_redundant(
    perf_dsn: str, dsn: str, conn_manager: ConnectionManager
) -> None:
    """`UNIQUE (email)` is not superseded by `(email, region)` — the composite does not
    enforce the same constraint. Confusing "serves the same queries" with "enforces the
    same rule" would talk someone into dropping a uniqueness guarantee."""
    setup = await asyncpg.connect(dsn)
    try:
        await setup.execute("DROP INDEX IF EXISTS idx_uniq_email, idx_email_region")
        await setup.execute("CREATE UNIQUE INDEX idx_uniq_email ON perf_users (email)")
        await setup.execute("CREATE INDEX idx_email_region ON perf_users (email, region)")
    finally:
        await setup.close()

    advice = await index_advise(conn_manager)
    assert "idx_uniq_email" not in {r["index"] for r in advice.redundant_indexes}

    cleanup = await asyncpg.connect(dsn)
    try:
        await cleanup.execute("DROP INDEX IF EXISTS idx_uniq_email, idx_email_region")
    finally:
        await cleanup.close()


async def test_detects_scan_hotspot(
    perf_dsn: str, dsn: str, conn_manager: ConnectionManager
) -> None:
    churn = await asyncpg.connect(dsn)
    try:
        for _ in range(3):
            await churn.execute("SELECT count(*) FROM perf_events WHERE status = 'rare'")
    finally:
        await churn.close()

    advice = await index_advise(conn_manager)
    tables = {h["table_name"] for h in advice.scan_hotspots}
    assert "perf_events" in tables


async def test_missing_pg_stat_statements_degrades_gracefully(
    perf_dsn: str, conn_manager: ConnectionManager
) -> None:
    """The test container has no pg_stat_statements (it needs shared_preload_libraries
    and a restart). The tool must still return catalog findings plus an explanation,
    not fail the call."""
    advice = await index_advise(conn_manager)
    assert advice.top_statements == []
    assert any("pg_stat_statements" in note for note in advice.notes)


async def test_result_is_json_serializable(perf_dsn: str, conn_manager: ConnectionManager) -> None:
    advice = await index_advise(conn_manager)
    json.dumps(advice.to_dict())


async def test_unused_index_advice_is_gated_on_observation_window(
    perf_dsn: str, dsn: str, conn_manager: ConnectionManager
) -> None:
    """Regression: the advisor recommended `DROP INDEX` on an index a query had used
    seconds earlier, because pg_stat counters lag and read a stale 0.

    Confidently wrong advice is worse than no advice — it discredits every other
    finding. The window is now measured and reported, and gates the recommendation.
    """
    setup = await asyncpg.connect(dsn)
    try:
        await setup.execute("DROP INDEX IF EXISTS idx_recently_used")
        await setup.execute("CREATE INDEX idx_recently_used ON perf_events (created_at)")
    finally:
        await setup.close()

    advice = await index_advise(conn_manager)
    assert advice.stats_window["observed_for"]
    for finding in advice.unused_indexes:
        # nothing on a fresh container may carry a bare DROP suggestion
        assert not finding["suggestion"].startswith("DROP INDEX"), finding
    assert any("LOW confidence" in note for note in advice.notes)

    cleanup = await asyncpg.connect(dsn)
    try:
        await cleanup.execute("DROP INDEX IF EXISTS idx_recently_used")
    finally:
        await cleanup.close()
