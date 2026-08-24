"""schema.inspect — table/column/index/constraint introspection from pg_catalog.

Reads pg_catalog/information_schema directly instead of shelling out to `psql \\d+` or
depending on SQLAlchemy's reflection: no extra dependency, no parsing psql's text
output, and it's the same system Postgres's own tools query.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pgops.connections import ConnectionManager
from pgops.errors import ErrorCode, PgopsError

Level = Literal["summary", "tables", "full"]

_TABLE_LIST_SQL = """
SELECT
    c.relname AS table_name,
    pg_total_relation_size(c.oid) AS total_bytes,
    c.reltuples::bigint AS estimated_rows
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'r'
  AND n.nspname = 'public'
  AND ($1::text IS NULL OR c.relname = $1)
ORDER BY c.relname
"""

_COLUMNS_SQL = """
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = $1
ORDER BY ordinal_position
"""

_CONSTRAINTS_SQL = """
SELECT conname, contype, pg_get_constraintdef(oid) AS definition
FROM pg_constraint
WHERE conrelid = $1::regclass
"""

_INDEXES_SQL = """
SELECT indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'public' AND tablename = $1
"""

_EXTENSIONS_SQL = """
SELECT extname, extversion FROM pg_extension ORDER BY extname
"""


@dataclass(slots=True)
class TableInfo:
    name: str
    total_bytes: int
    estimated_rows: int
    columns: list[dict[str, Any]] = field(default_factory=list)
    constraints: list[dict[str, Any]] = field(default_factory=list)
    indexes: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self, level: Level) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "total_bytes": self.total_bytes,
            "estimated_rows": self.estimated_rows,
        }
        if level in ("tables", "full"):
            d["columns"] = self.columns
        if level == "full":
            d["constraints"] = self.constraints
            d["indexes"] = self.indexes
        return d


@dataclass(slots=True)
class SchemaSnapshot:
    tables: list[TableInfo]
    extensions: list[dict[str, Any]]

    def to_dict(self, level: Level) -> dict[str, Any]:
        out: dict[str, Any] = {"tables": [t.to_dict(level) for t in self.tables]}
        if level == "full":
            out["extensions"] = self.extensions
        return out


async def schema_inspect(
    conn_manager: ConnectionManager,
    level: Level = "summary",
    table: str | None = None,
) -> SchemaSnapshot:
    async with conn_manager.readonly_pool.acquire() as conn:
        table_rows = await conn.fetch(_TABLE_LIST_SQL, table)
        if table is not None and not table_rows:
            raise PgopsError(
                ErrorCode.INVALID_ARGUMENT,
                f"table {table!r} not found in schema 'public'",
            )

        tables: list[TableInfo] = []
        for row in table_rows:
            info = TableInfo(
                name=row["table_name"],
                total_bytes=row["total_bytes"],
                estimated_rows=max(row["estimated_rows"], 0),
            )
            if level in ("tables", "full"):
                cols = await conn.fetch(_COLUMNS_SQL, row["table_name"])
                info.columns = [dict(c) for c in cols]
            if level == "full":
                cons = await conn.fetch(_CONSTRAINTS_SQL, row["table_name"])
                info.constraints = [dict(c) for c in cons]
                idxs = await conn.fetch(_INDEXES_SQL, row["table_name"])
                info.indexes = [dict(i) for i in idxs]
            tables.append(info)

        extensions: list[dict[str, Any]] = []
        if level == "full":
            ext_rows = await conn.fetch(_EXTENSIONS_SQL)
            extensions = [dict(e) for e in ext_rows]

    return SchemaSnapshot(tables=tables, extensions=extensions)
