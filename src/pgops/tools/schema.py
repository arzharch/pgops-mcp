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
from pgops.serialize import serialize_value

Level = Literal["summary", "tables", "full"]

_TABLE_LIST_SQL = """
SELECT
    c.oid AS table_oid,
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

# Everything below keys off the table's OID, never its name. Passing a name into
# `$1::regclass` (the obvious first approach) is broken: regclass parses its input as
# an *identifier expression*, so any table needing quoting — mixed case, a space, a
# dot — raises `InvalidNameError: invalid name syntax` instead of resolving. Quoting it
# ourselves would mean reimplementing Postgres identifier-quoting rules in Python. The
# OID is already in hand from the table list above, is unambiguous, and needs no
# quoting at all.
# Each takes the full OID *array* and is executed once for the whole snapshot, not once
# per table. The obvious loop-a-query-per-table shape is an N+1: `level="full"` over a
# 200-table schema would be 600 round trips, and round-trip latency — not Postgres —
# would dominate the response time. `= ANY($1::oid[])` collapses that to three.
_COLUMNS_SQL = """
SELECT a.attrelid AS table_oid,
       a.attname AS column_name,
       format_type(a.atttypid, a.atttypmod) AS data_type,
       NOT a.attnotnull AS is_nullable,
       pg_get_expr(d.adbin, d.adrelid) AS column_default
FROM pg_attribute a
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE a.attrelid = ANY($1::oid[]) AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attrelid, a.attnum
"""

# contype is Postgres's internal "char" type — asyncpg decodes it to a *bytes* value
# (b'p'), not a str. Expanded to a readable label in SQL rather than shipped raw: an
# agent consuming this shouldn't have to know that 'p' means primary key, and a bare
# byte is meaningless in a JSON response.
_CONSTRAINTS_SQL = """
SELECT conrelid AS table_oid,
       conname,
       CASE contype
           WHEN 'p' THEN 'primary_key'
           WHEN 'f' THEN 'foreign_key'
           WHEN 'u' THEN 'unique'
           WHEN 'c' THEN 'check'
           WHEN 'x' THEN 'exclusion'
           WHEN 't' THEN 'trigger'
           ELSE contype::text
       END AS constraint_type,
       pg_get_constraintdef(oid) AS definition
FROM pg_constraint
WHERE conrelid = ANY($1::oid[])
ORDER BY conrelid, conname
"""

_INDEXES_SQL = """
SELECT i.indrelid AS table_oid,
       c.relname AS indexname,
       pg_get_indexdef(i.indexrelid) AS indexdef
FROM pg_index i
JOIN pg_class c ON c.oid = i.indexrelid
WHERE i.indrelid = ANY($1::oid[])
ORDER BY i.indrelid, c.relname
"""

_EXTENSIONS_SQL = """
SELECT extname, extversion FROM pg_extension ORDER BY extname
"""


def _without_oid(record: Any) -> dict[str, Any]:
    """Drop the grouping key and JSON-normalize the rest.

    table_oid exists only to fan results back out to the right table — it's an internal
    Postgres detail, not something a client should key on (OIDs change on dump/restore).

    serialize_value is applied here rather than trusted to "probably be fine": catalog
    columns are full of types that don't survive `json.dumps` (Postgres's internal
    "char" type decodes to Python *bytes*, and this shipped broken until a test asserted
    on a real constraint row). MCP results are JSON, so anything that can't encode is a
    transport-layer failure — a class of bug worth removing structurally, not per column.
    """
    return {k: serialize_value(v) for k, v in record.items() if k != "table_oid"}


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
    async with conn_manager.acquire_readonly() as conn:
        table_rows = await conn.fetch(_TABLE_LIST_SQL, table)
        if table is not None and not table_rows:
            raise PgopsError(
                ErrorCode.INVALID_ARGUMENT,
                f"table {table!r} not found in schema 'public'",
            )

        by_oid: dict[int, TableInfo] = {
            row["table_oid"]: TableInfo(
                name=row["table_name"],
                total_bytes=row["total_bytes"],
                estimated_rows=max(row["estimated_rows"], 0),
            )
            for row in table_rows
        }
        oids = list(by_oid)

        if level in ("tables", "full") and oids:
            for rec in await conn.fetch(_COLUMNS_SQL, oids):
                by_oid[rec["table_oid"]].columns.append(_without_oid(rec))
        if level == "full" and oids:
            for rec in await conn.fetch(_CONSTRAINTS_SQL, oids):
                by_oid[rec["table_oid"]].constraints.append(_without_oid(rec))
            for rec in await conn.fetch(_INDEXES_SQL, oids):
                by_oid[rec["table_oid"]].indexes.append(_without_oid(rec))

        tables = list(by_oid.values())

        extensions: list[dict[str, Any]] = []
        if level == "full":
            ext_rows = await conn.fetch(_EXTENSIONS_SQL)
            extensions = [dict(e) for e in ext_rows]

    return SchemaSnapshot(tables=tables, extensions=extensions)
