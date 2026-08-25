"""query.read: classifier gate, row-limit enforcement, timeout cancellation."""

from __future__ import annotations

import pytest

from pgops.config import PgopsConfig
from pgops.connections import ConnectionManager
from pgops.errors import ErrorCode, PgopsError
from pgops.tools.query import query_read


async def test_read_returns_rows(conn_manager: ConnectionManager, config: PgopsConfig) -> None:
    result = await query_read(conn_manager, config, "SELECT * FROM items ORDER BY id", limit=10)
    assert result.row_count == 10
    assert result.truncated is True


async def test_limit_not_truncated_when_under_cap(
    conn_manager: ConnectionManager, config: PgopsConfig
) -> None:
    result = await query_read(conn_manager, config, "SELECT * FROM items", limit=300)
    assert result.row_count == 250
    assert result.truncated is False


async def test_limit_above_server_max_rejected(
    conn_manager: ConnectionManager, config: PgopsConfig
) -> None:
    with pytest.raises(PgopsError) as exc_info:
        await query_read(conn_manager, config, "SELECT * FROM items", limit=config.row_limits.max + 1)
    assert exc_info.value.code is ErrorCode.ROW_LIMIT_EXCEEDED


async def test_write_statement_refused(conn_manager: ConnectionManager, config: PgopsConfig) -> None:
    with pytest.raises(PgopsError) as exc_info:
        await query_read(conn_manager, config, "DELETE FROM items")
    assert exc_info.value.code is ErrorCode.CLASSIFICATION_REFUSED


async def test_runaway_query_is_cancelled_by_timeout(
    conn_manager: ConnectionManager, config: PgopsConfig
) -> None:
    # pg_sleep is volatile (it changes server timing state), so the volatility check
    # refuses it before the timeout can. Use a genuinely stable slow construct:
    # a large recursive CTE burns CPU without touching any function.
    with pytest.raises(PgopsError) as exc_info:
        await query_read(
            conn_manager,
            config,
            """
            WITH RECURSIVE t(n) AS (
                SELECT 1 UNION ALL SELECT n+1 FROM t WHERE n < 100000000
            )
            SELECT count(*) FROM t
            """,
            timeout_ms=200,
        )
    assert exc_info.value.code is ErrorCode.QUERY_TIMEOUT
