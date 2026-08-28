"""Structural schema diff → dependency-ordered change set.

The target schema is supplied as plain JSON (documented in TOOLS.md), not as a DDL
script: an agent describing *desired state* is far less error-prone than an agent
writing migration SQL, and it lets this module own the ordering and safety questions
rather than trusting whatever order the SQL arrived in.

**Ordering is the correctness requirement here.** Postgres will reject a change set
applied in the wrong order — a foreign key referencing a table that does not exist yet,
an index on a column added later in the same batch. Creations go outside-in
(tables → columns → constraints → indexes) and drops go strictly in reverse
(indexes → constraints → columns → tables), because a dependency must be created after
the thing it depends on and dropped before it.

Scope is deliberately narrow for v1: tables, columns, indexes, and constraints in the
`public` schema. Not covered — views, triggers, functions, partitions, enum changes.
Anything the target mentions that isn't understood produces an explicit refusal rather
than a silently incomplete plan, because a migration tool that quietly skips part of
your intent is worse than one that says "I don't handle that".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import sqlparse

from pgops.errors import ErrorCode, PgopsError


class ChangeKind(StrEnum):
    CREATE_TABLE = "create_table"
    DROP_TABLE = "drop_table"
    ADD_COLUMN = "add_column"
    DROP_COLUMN = "drop_column"
    ALTER_COLUMN_TYPE = "alter_column_type"
    SET_NOT_NULL = "set_not_null"
    DROP_NOT_NULL = "drop_not_null"
    CREATE_INDEX = "create_index"
    DROP_INDEX = "drop_index"
    ADD_CONSTRAINT = "add_constraint"
    DROP_CONSTRAINT = "drop_constraint"


# Creation order: a thing must exist before anything that depends on it.
# Drops run in the exact reverse, which is why this is one list and not two.
_ORDER = [
    ChangeKind.CREATE_TABLE,
    ChangeKind.ADD_COLUMN,
    ChangeKind.ALTER_COLUMN_TYPE,
    ChangeKind.SET_NOT_NULL,
    ChangeKind.DROP_NOT_NULL,
    ChangeKind.ADD_CONSTRAINT,
    ChangeKind.CREATE_INDEX,
    # --- destructive tail, reverse dependency order ---
    ChangeKind.DROP_INDEX,
    ChangeKind.DROP_CONSTRAINT,
    ChangeKind.DROP_COLUMN,
    ChangeKind.DROP_TABLE,
]

_DESTRUCTIVE = {
    ChangeKind.DROP_TABLE,
    ChangeKind.DROP_COLUMN,
    ChangeKind.DROP_CONSTRAINT,
    ChangeKind.DROP_INDEX,
}

_KNOWN_TABLE_KEYS = {"columns", "indexes", "constraints"}
_KNOWN_COLUMN_KEYS = {"type", "nullable", "default"}

# pgops's own bookkeeping, which is not part of the user's schema and must never appear
# in a diff. Without this, `allow_drops=True` with a target that (reasonably) doesn't
# mention it emits `DROP TABLE pgops_migrations` — the engine destroys the ledger
# recording the very migration it is running, and then fails trying to write the result.
# Found exactly that way: the apply crashed with
# `relation "pgops_migrations" does not exist` while marking itself finished.
INTERNAL_TABLES = {"pgops_migrations"}


@dataclass(slots=True)
class Change:
    kind: ChangeKind
    table: str
    sql: str
    target: str = ""
    destructive: bool = False
    data_loss_reason: str | None = None

    def __post_init__(self) -> None:
        """Every generated statement must be exactly one statement.

        This is the backstop for the whole module, and it exists because the fragment
        checks below can only cover the fields known today. Parts of the target spec —
        a column type, a DEFAULT expression, a constraint definition, an index column
        list — are SQL text that pgops does not parse; it interpolates them into DDL it
        builds. Without this invariant, a constraint definition of

            "CHECK (total_cents >= 0); DROP TABLE customers; --"

        produced a *single* Change whose `sql` dropped a table, which the planner then
        annotated `destructive: false, risk: medium` — because it classifies by
        ChangeKind, and the kind was ADD_CONSTRAINT. Both the risk summary and the
        rollback plan describe only the statement pgops thinks it emitted. Enforcing
        one-statement-per-Change at construction makes that class of mismatch
        unrepresentable rather than merely unlikely.
        """
        if len([st for st in sqlparse.split(self.sql) if st.strip()]) > 1:
            raise PgopsError(
                ErrorCode.INVALID_ARGUMENT,
                f"refusing to plan {self.kind.value} on {self.table!r}: the generated "
                f"statement contains more than one SQL statement",
                hint="a value in the target schema smuggled a statement separator into "
                "generated DDL; pass one expression per field",
            )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "kind": self.kind.value,
            "table": self.table,
            "sql": self.sql,
            "destructive": self.destructive,
        }
        if self.target:
            d["target"] = self.target
        if self.data_loss_reason:
            d["data_loss_reason"] = self.data_loss_reason
        return d


@dataclass(slots=True)
class ChangeSet:
    changes: list[Change] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def has_destructive(self) -> bool:
        return any(c.destructive for c in self.changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "changes": [c.to_dict() for c in self.changes],
            "destructive": self.has_destructive,
            "notes": self.notes,
        }


def _quote_ident(name: str) -> str:
    """Quote an identifier the way Postgres does — doubling embedded quotes.

    Always quoting (rather than only when it looks necessary) means mixed-case and
    reserved-word identifiers work without a special case, and removes any question of
    identifier injection through a table name in the target spec.
    """
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def _reject_smuggled_sql(field: str, text: str) -> None:
    """Refuse a target-schema fragment that is more than one SQL expression.

    These fragments are interpolated into DDL verbatim — pgops cannot type-check a
    Postgres type name or a CHECK expression, and pretending otherwise would mean
    reimplementing the parser. What it can do is insist that a field which is supposed
    to be *one expression* does not carry a statement boundary or a comment introducer
    that hides the rest of the line. `sqlparse.split` is used rather than a scan for
    ";" because a semicolon inside a string literal (DEFAULT ';') is legitimate and a
    naive check would reject it.
    """
    if len([st for st in sqlparse.split(text) if st.strip()]) > 1:
        raise PgopsError(
            ErrorCode.INVALID_ARGUMENT,
            f"{field} contains more than one SQL statement",
            hint="this field is interpolated into generated DDL and must be a single "
            "expression; statement separators are refused",
        )
    if "--" in text or "/*" in text:
        raise PgopsError(
            ErrorCode.INVALID_ARGUMENT,
            f"{field} contains a SQL comment",
            hint="comments in an interpolated fragment can hide the rest of the "
            "generated statement from review; remove them",
        )


def _index_columns(field: str, definition: Any) -> str:
    """Normalise an index definition to a column list, refusing shapes we don't handle.

    Previously any non-str value was passed to `", ".join(...)`, so a dict — the shape
    an agent reaches for first, {"columns": [...]} — silently joined its *keys* and
    planned `CREATE INDEX ... (columns)`. That reached the database before anything
    complained, and the resulting "column \"columns\" does not exist" pointed nowhere
    near the actual mistake.
    """
    if isinstance(definition, str):
        columns = definition
    elif isinstance(definition, list) and all(isinstance(c, str) for c in definition):
        columns = ", ".join(definition)
    else:
        raise PgopsError(
            ErrorCode.INVALID_ARGUMENT,
            f"{field} must be a column name string or a list of column names",
            hint='e.g. {"indexes": {"orders_region_idx": ["region", "created_at"]}}',
        )
    _reject_smuggled_sql(field, columns)
    return columns


def _validate_target(target: dict[str, Any]) -> None:
    if "tables" not in target:
        raise PgopsError(
            ErrorCode.INVALID_ARGUMENT,
            "target schema must have a 'tables' key",
            hint='e.g. {"tables": {"orders": {"columns": {"id": {"type": "bigint"}}}}}',
        )
    unsupported = set(target) - {"tables"}
    if unsupported:
        raise PgopsError(
            ErrorCode.INVALID_ARGUMENT,
            f"unsupported top-level keys in target schema: {sorted(unsupported)}",
            hint="v1 handles tables/columns/indexes/constraints only",
        )
    for table_name, spec in target["tables"].items():
        extra = set(spec) - _KNOWN_TABLE_KEYS
        if extra:
            raise PgopsError(
                ErrorCode.INVALID_ARGUMENT,
                f"table {table_name!r} has unsupported keys: {sorted(extra)}",
                hint=f"supported: {sorted(_KNOWN_TABLE_KEYS)}",
            )
        for col_name, col in spec.get("columns", {}).items():
            if not isinstance(col, dict) or "type" not in col:
                raise PgopsError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"column {table_name}.{col_name} must be an object with a 'type'",
                )
            col_extra = set(col) - _KNOWN_COLUMN_KEYS
            if col_extra:
                raise PgopsError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"column {table_name}.{col_name} has unsupported keys: {sorted(col_extra)}",
                    hint=f"supported: {sorted(_KNOWN_COLUMN_KEYS)}. A primary key or a "
                    f'foreign key goes in "constraints", not on the column.',
                )
            _reject_smuggled_sql(f"column {table_name}.{col_name} type", str(col["type"]))
            if col.get("default") is not None:
                _reject_smuggled_sql(
                    f"column {table_name}.{col_name} default", str(col["default"])
                )
        for cons_name, definition in spec.get("constraints", {}).items():
            if not isinstance(definition, str):
                raise PgopsError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"constraint {table_name}.{cons_name} must be a definition string",
                    hint='e.g. {"constraints": {"orders_pkey": "PRIMARY KEY (id)"}}',
                )
            _reject_smuggled_sql(f"constraint {table_name}.{cons_name}", definition)
        for idx_name, definition in spec.get("indexes", {}).items():
            _index_columns(f"index {table_name}.{idx_name}", definition)


def _column_definition(name: str, spec: dict[str, Any]) -> str:
    parts = [_quote_ident(name), spec["type"]]
    if spec.get("default") is not None:
        parts.append(f"DEFAULT {spec['default']}")
    if spec.get("nullable") is False:
        parts.append("NOT NULL")
    return " ".join(parts)


def diff_schema(
    live: dict[str, Any],
    target: dict[str, Any],
    *,
    allow_drops: bool = False,
) -> ChangeSet:
    """Compare a live schema snapshot against a target definition.

    `live` is the shape produced by `schema.inspect(level="full")`.

    `allow_drops` defaults to False, and that default is the important part: a target
    spec that simply *omits* a table is far more likely to be a partial description than
    a request to drop it. Deleting data because something wasn't mentioned is exactly
    the failure this project exists to prevent, so removals are only emitted when asked
    for explicitly.
    """
    _validate_target(target)
    changeset = ChangeSet()

    live_tables = {t["name"]: t for t in live.get("tables", []) if t["name"] not in INTERNAL_TABLES}
    target_tables = target["tables"]

    claimed = set(target_tables) & INTERNAL_TABLES
    if claimed:
        raise PgopsError(
            ErrorCode.INVALID_ARGUMENT,
            f"{sorted(claimed)} is managed by pgops and cannot be migrated",
            hint="pgops_migrations is the migration ledger; leave it out of the target",
        )

    for table_name, spec in target_tables.items():
        if table_name not in live_tables:
            _emit_create_table(changeset, table_name, spec)
        else:
            _diff_existing_table(changeset, table_name, spec, live_tables[table_name], allow_drops)

    if allow_drops:
        for table_name in live_tables:
            if table_name not in target_tables:
                changeset.changes.append(
                    Change(
                        kind=ChangeKind.DROP_TABLE,
                        table=table_name,
                        sql=f"DROP TABLE {_quote_ident(table_name)}",
                        destructive=True,
                        data_loss_reason=(
                            f"dropping {table_name} destroys all of its rows; this cannot "
                            "be reversed by a down-migration"
                        ),
                    )
                )
    else:
        missing = sorted(set(live_tables) - set(target_tables))
        if missing:
            changeset.notes.append(
                f"tables present in the database but absent from the target were left "
                f"alone: {missing}. Pass allow_drops=true to remove them."
            )

    changeset.changes.sort(key=lambda c: _ORDER.index(c.kind))
    return changeset


def _emit_create_table(changeset: ChangeSet, table: str, spec: dict[str, Any]) -> None:
    columns = spec.get("columns", {})
    if not columns:
        raise PgopsError(
            ErrorCode.INVALID_ARGUMENT, f"new table {table!r} must define at least one column"
        )
    cols_sql = ", ".join(_column_definition(n, c) for n, c in columns.items())
    changeset.changes.append(
        Change(
            kind=ChangeKind.CREATE_TABLE,
            table=table,
            sql=f"CREATE TABLE {_quote_ident(table)} ({cols_sql})",
        )
    )
    _emit_constraints_and_indexes(
        changeset, table, spec, existing_constraints=set(), existing_indexes=set()
    )


def _emit_constraints_and_indexes(
    changeset: ChangeSet,
    table: str,
    spec: dict[str, Any],
    existing_constraints: set[str],
    existing_indexes: set[str],
) -> None:
    for name, definition in spec.get("constraints", {}).items():
        if name in existing_constraints:
            continue
        changeset.changes.append(
            Change(
                kind=ChangeKind.ADD_CONSTRAINT,
                table=table,
                target=name,
                sql=(
                    f"ALTER TABLE {_quote_ident(table)} "
                    f"ADD CONSTRAINT {_quote_ident(name)} {definition}"
                ),
            )
        )
    for name, definition in spec.get("indexes", {}).items():
        if name in existing_indexes:
            continue
        columns = _index_columns(f"index {table}.{name}", definition)
        changeset.changes.append(
            Change(
                kind=ChangeKind.CREATE_INDEX,
                table=table,
                target=name,
                sql=(f"CREATE INDEX {_quote_ident(name)} ON {_quote_ident(table)} ({columns})"),
            )
        )


def _diff_existing_table(
    changeset: ChangeSet,
    table: str,
    spec: dict[str, Any],
    live_table: dict[str, Any],
    allow_drops: bool,
) -> None:
    live_columns = {c["column_name"]: c for c in live_table.get("columns", [])}
    target_columns = spec.get("columns", {})

    for col_name, col_spec in target_columns.items():
        if col_name not in live_columns:
            changeset.changes.append(
                Change(
                    kind=ChangeKind.ADD_COLUMN,
                    table=table,
                    target=col_name,
                    sql=(
                        f"ALTER TABLE {_quote_ident(table)} ADD COLUMN "
                        f"{_column_definition(col_name, col_spec)}"
                    ),
                )
            )
            continue

        live_col = live_columns[col_name]
        if not _types_match(live_col["data_type"], col_spec["type"]):
            changeset.changes.append(
                Change(
                    kind=ChangeKind.ALTER_COLUMN_TYPE,
                    table=table,
                    target=col_name,
                    sql=(
                        f"ALTER TABLE {_quote_ident(table)} ALTER COLUMN "
                        f"{_quote_ident(col_name)} TYPE {col_spec['type']}"
                    ),
                )
            )
        target_nullable = col_spec.get("nullable")
        if target_nullable is not None and bool(live_col["is_nullable"]) != bool(target_nullable):
            if target_nullable is False:
                changeset.changes.append(
                    Change(
                        kind=ChangeKind.SET_NOT_NULL,
                        table=table,
                        target=col_name,
                        sql=(
                            f"ALTER TABLE {_quote_ident(table)} ALTER COLUMN "
                            f"{_quote_ident(col_name)} SET NOT NULL"
                        ),
                    )
                )
            else:
                changeset.changes.append(
                    Change(
                        kind=ChangeKind.DROP_NOT_NULL,
                        table=table,
                        target=col_name,
                        sql=(
                            f"ALTER TABLE {_quote_ident(table)} ALTER COLUMN "
                            f"{_quote_ident(col_name)} DROP NOT NULL"
                        ),
                    )
                )

    if allow_drops and target_columns:
        for col_name in live_columns:
            if col_name not in target_columns:
                changeset.changes.append(
                    Change(
                        kind=ChangeKind.DROP_COLUMN,
                        table=table,
                        target=col_name,
                        sql=(
                            f"ALTER TABLE {_quote_ident(table)} DROP COLUMN "
                            f"{_quote_ident(col_name)}"
                        ),
                        destructive=True,
                        data_loss_reason=(
                            f"{table}.{col_name} holds data that a down-migration cannot "
                            "restore — the values are gone once VACUUM reclaims them"
                        ),
                    )
                )

    _emit_constraints_and_indexes(
        changeset,
        table,
        spec,
        existing_constraints={c["conname"] for c in live_table.get("constraints", [])},
        existing_indexes={i["indexname"] for i in live_table.get("indexes", [])},
    )


# Postgres reports types in its own canonical spelling, which rarely matches what a
# human writes. Comparing raw strings would emit a spurious (and rewriting!) ALTER TYPE
# for `int` vs `integer` — an expensive no-op on a large table.
_TYPE_ALIASES = {
    "int": "integer",
    "int4": "integer",
    "int8": "bigint",
    "int2": "smallint",
    "serial": "integer",
    "bigserial": "bigint",
    "bool": "boolean",
    "float8": "double precision",
    "float4": "real",
    "varchar": "character varying",
    "char": "character",
    "timestamptz": "timestamp with time zone",
    "timetz": "time with time zone",
    "decimal": "numeric",
}


def _normalize_type(type_name: str) -> str:
    base = type_name.strip().lower()
    # strip a length/precision qualifier: varchar(255) and varchar differ in width, but
    # treating them as different types here would be a false positive on most specs
    if "(" in base:
        base = base.split("(", 1)[0].strip()
    return _TYPE_ALIASES.get(base, base)


def _types_match(live_type: str, target_type: str) -> bool:
    return _normalize_type(live_type) == _normalize_type(target_type)


# The JSON Schema for `target`, published to MCP clients as migration.plan's parameter
# schema.
#
# It exists because the parameter was typed `dict[str, Any]`, which reaches the client as
# `{"type": "object", "additionalProperties": true}` — the flagship tool of this server
# telling the model, in the one place the model is guaranteed to read, that any object
# will do. The shape was documented only in prose in the description, so a client had to
# infer the grammar and find out it was wrong from an error. Observed first-guess
# failures: `{"primary_key": true}` on a column, and `{"columns": [...]}` as an index
# definition.
#
# Kept beside _validate_target deliberately: this schema and that function describe the
# same grammar, and separating them is how they drift.
TARGET_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "The desired end state of the schema, not a list of changes — the diff is "
        "computed. Anything not mentioned is left alone."
    ),
    "properties": {
        "tables": {
            "type": "object",
            "description": "Keyed by table name.",
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "columns": {
                        "type": "object",
                        "description": "Keyed by column name.",
                        "additionalProperties": {
                            "type": "object",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "description": "Postgres type, e.g. 'text', "
                                    "'bigint', 'numeric(14,2)', 'timestamptz'.",
                                },
                                "nullable": {"type": "boolean", "default": True},
                                "default": {
                                    "type": "string",
                                    "description": "SQL expression for the DEFAULT, "
                                    "e.g. '0' or \"'pending'\" or 'now()'. A constant "
                                    "default is a catalog change; a volatile one "
                                    "rewrites the table.",
                                },
                            },
                            "required": ["type"],
                            "additionalProperties": False,
                        },
                    },
                    "constraints": {
                        "type": "object",
                        "description": "Keyed by constraint name; the value is the "
                        "definition, e.g. 'PRIMARY KEY (id)', 'UNIQUE (email)', "
                        "'CHECK (qty > 0)', 'FOREIGN KEY (order_id) REFERENCES "
                        "orders(id)'. Primary and foreign keys go here, not on the "
                        "column.",
                        "additionalProperties": {"type": "string"},
                    },
                    "indexes": {
                        "type": "object",
                        "description": "Keyed by index name; the value is the column "
                        "list.",
                        "additionalProperties": {
                            "anyOf": [
                                {"type": "string"},
                                {"type": "array", "items": {"type": "string"}},
                            ]
                        },
                    },
                },
                "additionalProperties": False,
            },
        }
    },
    "required": ["tables"],
    "additionalProperties": False,
}
