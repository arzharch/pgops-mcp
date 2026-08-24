"""schema.inspect: levels, scoping, and the identifier-quoting regression."""

from __future__ import annotations

import json

import asyncpg
import pytest

from pgops.connections import ConnectionManager
from pgops.errors import ErrorCode, PgopsError
from pgops.tools.schema import schema_inspect


async def test_summary_level_lists_tables(conn_manager: ConnectionManager) -> None:
    snapshot = await schema_inspect(conn_manager, level="summary")
    names = {t.name for t in snapshot.tables}
    assert "items" in names
    # summary carries sizes but no column detail
    items = next(t for t in snapshot.tables if t.name == "items")
    assert items.total_bytes > 0
    assert items.columns == []


async def test_tables_level_includes_columns(conn_manager: ConnectionManager) -> None:
    snapshot = await schema_inspect(conn_manager, level="tables", table="items")
    items = snapshot.tables[0]
    by_name = {c["column_name"]: c for c in items.columns}
    assert set(by_name) == {"id", "name"}
    assert by_name["name"]["is_nullable"] is False
    assert by_name["id"]["column_default"].startswith("nextval(")


async def test_full_level_includes_constraints_and_indexes(
    conn_manager: ConnectionManager,
) -> None:
    snapshot = await schema_inspect(conn_manager, level="full", table="items")
    items = snapshot.tables[0]
    assert any(c["constraint_type"] == "primary_key" for c in items.constraints), items.constraints
    assert any("items_pkey" in i["indexname"] for i in items.indexes), items.indexes
    assert snapshot.extensions  # at least plpgsql is always installed


async def test_full_snapshot_is_json_serializable(conn_manager: ConnectionManager) -> None:
    """Regression: pg_constraint.contype is Postgres's internal "char" type, which
    asyncpg decodes to *bytes*. Shipping it raw made `schema.inspect(level="full")`
    fail at the JSON transport layer for every caller — while `level="summary"` and the
    selfcheck (which only reads summary) both passed. Serialization is now asserted at
    the level that actually carries catalog types."""
    snapshot = await schema_inspect(conn_manager, level="full")
    json.dumps(snapshot.to_dict("full"))


async def test_unknown_table_is_structured_error(conn_manager: ConnectionManager) -> None:
    with pytest.raises(PgopsError) as exc_info:
        await schema_inspect(conn_manager, level="full", table="no_such_table")
    assert exc_info.value.code is ErrorCode.INVALID_ARGUMENT


async def test_table_name_requiring_quoting(conn_manager: ConnectionManager, dsn: str) -> None:
    """Regression: the first implementation passed the table name into `$1::regclass`,
    which parses its argument as an identifier expression — any name needing quotes
    raised a raw `InvalidNameError` that escaped the tool layer entirely. Keying off
    pg_class.oid instead removes the name-parsing step altogether."""
    setup = await asyncpg.connect(dsn)
    try:
        await setup.execute('CREATE TABLE IF NOT EXISTS "Order Items" (id int, "Full Name" text)')
    finally:
        await setup.close()

    try:
        snapshot = await schema_inspect(conn_manager, level="full", table="Order Items")
        table = snapshot.tables[0]
        assert table.name == "Order Items"
        assert {c["column_name"] for c in table.columns} == {"id", "Full Name"}
    finally:
        cleanup = await asyncpg.connect(dsn)
        try:
            await cleanup.execute('DROP TABLE IF EXISTS "Order Items"')
        finally:
            await cleanup.close()
