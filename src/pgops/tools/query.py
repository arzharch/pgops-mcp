"""query.read — the only way an agent touches data before Phase 2 lands query.write.

Row-limit enforcement deliberately does NOT rewrite the SQL text (e.g. wrapping it in
`SELECT * FROM (...) sub LIMIT n`). That approach breaks on EXPLAIN, statements with a
trailing semicolon, CTEs that reference outer aliases, and is one more place untrusted
SQL gets string-manipulated right before execution. Instead we open a server-side
cursor and only ever `.fetch(limit)` from it — Postgres itself stops producing rows
past that point; the original SQL text reaches the server untouched.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import asyncpg

from pgops.classifier import classify
from pgops.config import PgopsConfig
from pgops.connections import ConnectionManager
from pgops.errors import ErrorCode, PgopsError
from pgops.serialize import serialize_record


@dataclass(slots=True)
class QueryReadResult:
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool
    duration_ms: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "duration_ms": round(self.duration_ms, 2),
        }


async def query_read(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    sql: str,
    limit: int | None = None,
    timeout_ms: int | None = None,
) -> QueryReadResult:
    classification = classify(sql)
    if not classification.is_read:
        raise PgopsError(
            ErrorCode.CLASSIFICATION_REFUSED,
            f"statement classified as {classification.effective_gate_class.value} "
            f"({classification.reason}); query.read only accepts pure reads",
            hint="use query.write once Phase 2 lands, or rewrite as a SELECT/WITH/EXPLAIN",
        )

    resolved_limit = config.row_limits.resolve(limit)
    resolved_timeout = config.timeouts.resolve(timeout_ms)

    start = time.monotonic()
    try:
        async with conn_manager.readonly_pool.acquire() as conn, conn.transaction():
            await conn.execute(f"SET LOCAL statement_timeout = {resolved_timeout}")
            cursor = await conn.cursor(sql)
            # fetch limit+1 so we can report `truncated` without a second round trip
            records = await cursor.fetch(resolved_limit + 1)
    except asyncpg.QueryCanceledError as exc:
        raise PgopsError(
            ErrorCode.QUERY_TIMEOUT,
            f"statement exceeded {resolved_timeout}ms and was cancelled",
            hint="narrow the query or pass a higher timeout_ms (server max applies)",
        ) from exc
    except asyncpg.PostgresError as exc:
        raise PgopsError(ErrorCode.INVALID_ARGUMENT, str(exc)) from exc

    duration_ms = (time.monotonic() - start) * 1000
    truncated = len(records) > resolved_limit
    rows = [serialize_record(r) for r in records[:resolved_limit]]
    return QueryReadResult(rows=rows, row_count=len(rows), truncated=truncated, duration_ms=duration_ms)
