"""JSON-safe conversion for asyncpg row values.

MCP tool results are JSON. asyncpg hands back native Python types that aren't all
JSON-serializable as-is (Decimal, datetime, UUID, bytes, asyncpg.Range) — this is the
one place that mapping happens, so `query.read` and every future tool that returns rows
(db.health, schema.inspect) stay consistent instead of each inventing its own str().
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any
from uuid import UUID

import asyncpg


def serialize_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        # str, not float: avoids silently losing precision on money/numeric columns.
        return str(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, asyncpg.Range):
        return {
            "lower": serialize_value(value.lower),
            "upper": serialize_value(value.upper),
            "lower_inc": value.lower_inc,
            "upper_inc": value.upper_inc,
        }
    if isinstance(value, (list, tuple)):
        return [serialize_value(v) for v in value]
    if isinstance(value, dict):
        return {k: serialize_value(v) for k, v in value.items()}
    # asyncpg decodes jsonb to plain dict/list/str/etc already; anything else
    # unrecognized (custom enum/composite types) — stringify rather than fail the
    # whole response over one odd column.
    return str(value)


def serialize_record(record: asyncpg.Record) -> dict[str, Any]:
    return {key: serialize_value(value) for key, value in record.items()}
