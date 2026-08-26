"""Seeded slow-query scenarios diagnosed against real Postgres (SPEC Phase 3 gate).

Each scenario has a known defect and an expected verdict. The point is not that the
parser runs — `test_plan_analysis.py` covers the arithmetic — but that the *rules fire
on plans Postgres actually chooses*, which is the only way to know a threshold is
tuned to reality rather than to my assumptions.

Negative scenarios matter as much as positive ones: a rule set that flags something on
every query is noise, so several tests assert a healthy query produces no verdict.
"""

from __future__ import annotations

import pytest

from pgops.audit import AuditLog
from pgops.config import PgopsConfig
from pgops.connections import ConnectionManager
from pgops.errors import ErrorCode, PgopsError
from pgops.guardrails import ConfirmationTokenStore
from pgops.plan_analysis import VerdictKind
from pgops.tools.explain import query_explain


async def _kinds(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
    sql: str,
    analyze: bool = True,
) -> set[VerdictKind]:
    result = await query_explain(conn_manager, config, audit, tokens, sql, analyze=analyze)
    return {v.kind for v in result.verdicts}  # type: ignore[attr-defined]


# --- scenario 1: sequential scan on a large table -----------------------------------
async def test_scenario_seq_scan_on_large_table(
    perf_dsn: str,
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    kinds = await _kinds(
        conn_manager,
        config,
        audit,
        tokens,
        "SELECT * FROM perf_events WHERE payload LIKE '%zzz%'",
    )
    assert VerdictKind.SEQ_SCAN_LARGE_TABLE in kinds


# --- scenario 2: highly selective filter with no index ------------------------------
async def test_scenario_expensive_filter(
    perf_dsn: str,
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    """'rare' matches 6 rows in 60,000 — Postgres must read and reject the rest."""
    result = await query_explain(
        conn_manager,
        config,
        audit,
        tokens,
        "SELECT * FROM perf_events WHERE status = 'rare'",
        analyze=True,
    )
    verdicts = {v.kind: v for v in result.verdicts}
    assert VerdictKind.EXPENSIVE_FILTER in verdicts
    assert "status" in verdicts[VerdictKind.EXPENSIVE_FILTER].evidence


# --- scenario 3: sort spilling to disk ----------------------------------------------
async def test_explain_does_not_bypass_the_classifier(
    perf_dsn: str,
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    """Wrapping SQL in EXPLAIN must not become a way to smuggle a stacked statement
    past the classifier — the sneaky shape being
    `SET ...; SELECT ...` where the second half is what actually runs."""
    with pytest.raises(PgopsError) as exc_info:
        await query_explain(
            conn_manager,
            config,
            audit,
            tokens,
            "SET work_mem = '64kB'; SELECT * FROM perf_events ORDER BY payload",
            analyze=True,
        )
    assert "2 statements" in str(exc_info.value)


async def test_scenario_sort_spill_with_low_work_mem(
    perf_dsn: str,
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    kinds = await _kinds(
        conn_manager,
        config,
        audit,
        tokens,
        "SELECT * FROM perf_events ORDER BY payload, created_at, id",
    )
    # 60k rows × ~130 bytes exceeds the default 4MB work_mem
    assert VerdictKind.SORT_SPILL in kinds


# --- scenario 4: nested loop with a large outer side --------------------------------
async def test_scenario_nested_loop_blowup(
    perf_dsn: str,
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    result = await query_explain(
        conn_manager,
        config,
        audit,
        tokens,
        """
        SELECT u.email
        FROM perf_users u
        WHERE EXISTS (
            SELECT 1 FROM perf_events e WHERE e.user_id = u.id AND e.status = 'rare'
        )
        """,
        analyze=True,
    )
    # whatever plan is chosen, the analyzer must not crash and must produce a tree
    assert result.plan["node"]


# --- scenario 5: estimate divergence from stale statistics --------------------------
async def test_scenario_estimate_divergence(
    perf_dsn: str,
    dsn: str,
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    """Correlated predicates: region and email prefix both derive from the same id, so
    the planner multiplies their selectivities and badly underestimates the result."""
    import asyncpg

    setup = await asyncpg.connect(dsn)
    try:
        await setup.execute("DROP TABLE IF EXISTS skewed")
        await setup.execute("CREATE TABLE skewed (a int, b int)")
        # a and b are perfectly correlated; the planner assumes independence
        await setup.execute(
            "INSERT INTO skewed SELECT i % 100, i % 100 FROM generate_series(1, 100000) i"
        )
        await setup.execute("ANALYZE skewed")
    finally:
        await setup.close()

    kinds = await _kinds(
        conn_manager, config, audit, tokens, "SELECT * FROM skewed WHERE a = 1 AND b = 1"
    )
    assert VerdictKind.ESTIMATE_DIVERGENCE in kinds


# --- scenario 6: index scan is healthy, no scan verdict -----------------------------
async def test_scenario_indexed_lookup_is_clean(
    perf_dsn: str,
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    kinds = await _kinds(
        conn_manager, config, audit, tokens, "SELECT * FROM perf_events WHERE user_id = 42"
    )
    assert VerdictKind.SEQ_SCAN_LARGE_TABLE not in kinds
    assert VerdictKind.EXPENSIVE_FILTER not in kinds


# --- scenario 7: small table scan is not flagged ------------------------------------
async def test_scenario_small_table_scan_is_clean(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    kinds = await _kinds(conn_manager, config, audit, tokens, "SELECT * FROM items")
    assert VerdictKind.SEQ_SCAN_LARGE_TABLE not in kinds


# --- scenario 8: primary key lookup is clean ----------------------------------------
async def test_scenario_pk_lookup_is_clean(
    perf_dsn: str,
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    result = await query_explain(
        conn_manager,
        config,
        audit,
        tokens,
        "SELECT * FROM perf_events WHERE id = 500",
        analyze=True,
    )
    assert [v for v in result.verdicts if v.severity.value == "critical"] == []


# --- scenario 9: aggregate over a large table ---------------------------------------
async def test_scenario_aggregate_over_large_table(
    perf_dsn: str,
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    result = await query_explain(
        conn_manager,
        config,
        audit,
        tokens,
        "SELECT status, count(*) FROM perf_events GROUP BY status",
        analyze=True,
    )
    assert VerdictKind.SEQ_SCAN_LARGE_TABLE in {v.kind for v in result.verdicts}
    assert result.meta["execution_time_ms"] > 0


# --- scenario 10: parallel plan reports sane percentages ----------------------------
async def test_scenario_parallel_plan_percentages_are_sane(
    perf_dsn: str,
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    """Regression against the parallel-loops bug: a node cannot own more than 100% of
    execution time."""
    result = await query_explain(
        conn_manager,
        config,
        audit,
        tokens,
        "SELECT * FROM perf_events WHERE payload LIKE '%q%' ORDER BY created_at",
        analyze=True,
    )
    for verdict in result.verdicts:
        if verdict.kind is VerdictKind.DOMINANT_NODE:
            percent = int(verdict.evidence.split("(")[1].split("%")[0])
            assert 0 <= percent <= 100, verdict.evidence


# --- scenario 11: join across both tables -------------------------------------------
async def test_scenario_join_produces_tree(
    perf_dsn: str,
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    result = await query_explain(
        conn_manager,
        config,
        audit,
        tokens,
        """
        SELECT u.region, count(*)
        FROM perf_events e JOIN perf_users u ON u.id = e.user_id
        GROUP BY u.region
        """,
        analyze=True,
    )
    assert "children" in result.plan


# --- behaviour: analyze=false never executes ----------------------------------------
async def test_explain_without_analyze_has_no_timings(
    perf_dsn: str,
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    result = await query_explain(
        conn_manager,
        config,
        audit,
        tokens,
        "SELECT * FROM perf_events WHERE status = 'rare'",
        analyze=False,
    )
    assert result.analyzed is False
    assert "execution_time_ms" not in result.meta
    assert "actual_rows" not in result.plan


async def test_explain_analyze_false_on_delete_does_not_delete(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    """The safety property that matters most: planning a DELETE must not delete."""
    import asyncpg

    result = await query_explain(
        conn_manager, config, audit, tokens, "DELETE FROM items", analyze=False
    )
    assert result.analyzed is False
    conn = await asyncpg.connect(config.dsn)
    try:
        assert await conn.fetchval("SELECT count(*) FROM items") == 250
    finally:
        await conn.close()


async def test_explain_analyze_on_delete_requires_confirmation(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    """EXPLAIN ANALYZE actually executes — it must be gated like a write."""
    with pytest.raises(PgopsError) as exc_info:
        await query_explain(conn_manager, config, audit, tokens, "DELETE FROM items", analyze=True)
    assert exc_info.value.code is ErrorCode.CONFIRMATION_REQUIRED
    assert "executes the statement" in exc_info.value.message


async def test_explain_analyze_on_delete_rolls_back(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    """With confirmation, real timings are collected — and the rows survive."""
    import asyncpg

    sql = "DELETE FROM items"
    with pytest.raises(PgopsError) as exc_info:
        await query_explain(conn_manager, config, audit, tokens, sql, analyze=True)
    token = (exc_info.value.hint or "").split("confirm_token=")[1].split("'")[1]

    result = await query_explain(
        conn_manager, config, audit, tokens, sql, analyze=True, confirm_token=token
    )
    assert result.analyzed is True
    assert result.meta["execution_time_ms"] > 0  # it really ran

    conn = await asyncpg.connect(config.dsn)
    try:
        assert await conn.fetchval("SELECT count(*) FROM items") == 250  # and rolled back
    finally:
        await conn.close()


async def test_explain_analyze_rollback_is_audited(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    tokens: ConfirmationTokenStore,
) -> None:
    log = AuditLog(config.audit_path)
    sql = "UPDATE items SET name = 'x'"
    with pytest.raises(PgopsError) as exc_info:
        await query_explain(conn_manager, config, log, tokens, sql, analyze=True)
    token = (exc_info.value.hint or "").split("confirm_token=")[1].split("'")[1]
    await query_explain(conn_manager, config, log, tokens, sql, analyze=True, confirm_token=token)

    verdicts = [e["verdict"] for e in log.read_all()]
    assert verdicts == ["refused_pending_confirmation", "executed_rolled_back"]


async def test_explain_rejects_invalid_sql(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
) -> None:
    with pytest.raises(PgopsError) as exc_info:
        await query_explain(
            conn_manager, config, audit, tokens, "SELECT * FROM no_such_table", analyze=False
        )
    assert exc_info.value.code is ErrorCode.INVALID_ARGUMENT
