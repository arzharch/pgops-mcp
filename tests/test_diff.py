"""Schema diff: ordering, type normalization, and the drop-safety default."""

from __future__ import annotations

import pytest

from pgops.errors import ErrorCode, PgopsError
from pgops.migrations.diff import Change, ChangeKind, diff_schema

LIVE = {
    "tables": [
        {
            "name": "orders",
            "columns": [
                {"column_name": "id", "data_type": "bigint", "is_nullable": False},
                {"column_name": "status", "data_type": "text", "is_nullable": False},
                {"column_name": "note", "data_type": "character varying", "is_nullable": True},
            ],
            "constraints": [{"conname": "orders_pkey", "constraint_type": "primary_key"}],
            "indexes": [{"indexname": "orders_pkey"}],
        }
    ]
}


def test_no_changes_when_target_matches() -> None:
    target = {"tables": {"orders": {"columns": {"id": {"type": "bigint"}}}}}
    assert diff_schema(LIVE, target).changes == []


def test_add_missing_column() -> None:
    target = {"tables": {"orders": {"columns": {"total": {"type": "integer"}}}}}
    changes = diff_schema(LIVE, target).changes
    assert len(changes) == 1
    assert changes[0].kind is ChangeKind.ADD_COLUMN
    assert changes[0].sql == 'ALTER TABLE "orders" ADD COLUMN "total" integer'


def test_create_missing_table() -> None:
    target = {"tables": {"invoices": {"columns": {"id": {"type": "bigint", "nullable": False}}}}}
    changes = diff_schema(LIVE, target).changes
    assert changes[0].kind is ChangeKind.CREATE_TABLE
    assert 'CREATE TABLE "invoices"' in changes[0].sql
    assert "NOT NULL" in changes[0].sql


@pytest.mark.parametrize(
    "live_type, target_type",
    [
        ("integer", "int"),
        ("integer", "int4"),
        ("bigint", "int8"),
        ("character varying", "varchar"),
        ("timestamp with time zone", "timestamptz"),
        ("boolean", "bool"),
        ("numeric", "decimal"),
        ("character varying", "varchar(255)"),
    ],
)
def test_type_aliases_do_not_produce_spurious_rewrites(live_type: str, target_type: str) -> None:
    """Postgres reports canonical type names. Comparing raw strings would emit an
    ALTER TYPE — a full table rewrite — for `int` vs `integer`, which is identical."""
    live = {
        "tables": [
            {
                "name": "t",
                "columns": [{"column_name": "c", "data_type": live_type, "is_nullable": True}],
                "constraints": [],
                "indexes": [],
            }
        ]
    }
    target = {"tables": {"t": {"columns": {"c": {"type": target_type}}}}}
    assert diff_schema(live, target).changes == []


def test_genuine_type_change_is_detected() -> None:
    target = {"tables": {"orders": {"columns": {"status": {"type": "integer"}}}}}
    changes = diff_schema(LIVE, target).changes
    assert changes[0].kind is ChangeKind.ALTER_COLUMN_TYPE


def test_nullability_change_detected() -> None:
    target = {"tables": {"orders": {"columns": {"note": {"type": "varchar", "nullable": False}}}}}
    changes = diff_schema(LIVE, target).changes
    assert changes[0].kind is ChangeKind.SET_NOT_NULL


def test_drops_are_not_emitted_by_default() -> None:
    """A target that omits a table is far more likely to be a partial description than
    a request to destroy it. Deleting data because something went unmentioned is the
    exact failure this project exists to prevent."""
    target = {"tables": {"invoices": {"columns": {"id": {"type": "bigint"}}}}}
    result = diff_schema(LIVE, target)
    assert not any(c.kind is ChangeKind.DROP_TABLE for c in result.changes)
    assert any("orders" in note for note in result.notes)


def test_drops_emitted_when_explicitly_allowed() -> None:
    target = {"tables": {"invoices": {"columns": {"id": {"type": "bigint"}}}}}
    result = diff_schema(LIVE, target, allow_drops=True)
    drops = [c for c in result.changes if c.kind is ChangeKind.DROP_TABLE]
    assert len(drops) == 1
    assert drops[0].destructive is True
    assert drops[0].data_loss_reason is not None


def test_dropped_column_carries_data_loss_reason() -> None:
    target = {"tables": {"orders": {"columns": {"id": {"type": "bigint"}}}}}
    result = diff_schema(LIVE, target, allow_drops=True)
    drops = [c for c in result.changes if c.kind is ChangeKind.DROP_COLUMN]
    assert {d.target for d in drops} == {"status", "note"}
    assert all(d.data_loss_reason for d in drops)


def test_creation_order_is_dependency_safe() -> None:
    """Postgres rejects an index on a column that does not exist yet, so ordering is a
    correctness requirement, not a cosmetic one."""
    target = {
        "tables": {
            "invoices": {
                "columns": {"id": {"type": "bigint"}, "code": {"type": "text"}},
                "indexes": {"idx_code": "code"},
                "constraints": {"invoices_pk": "PRIMARY KEY (id)"},
            }
        }
    }
    kinds = [c.kind for c in diff_schema(LIVE, target).changes]
    assert kinds.index(ChangeKind.CREATE_TABLE) < kinds.index(ChangeKind.ADD_CONSTRAINT)
    assert kinds.index(ChangeKind.ADD_CONSTRAINT) < kinds.index(ChangeKind.CREATE_INDEX)


def test_drop_order_is_reverse_of_creation() -> None:
    """Indexes and constraints must go before the column they depend on."""
    target = {"tables": {"orders": {"columns": {"id": {"type": "bigint"}}}}}
    kinds = [c.kind for c in diff_schema(LIVE, target, allow_drops=True).changes]
    if ChangeKind.DROP_INDEX in kinds and ChangeKind.DROP_COLUMN in kinds:
        assert kinds.index(ChangeKind.DROP_INDEX) < kinds.index(ChangeKind.DROP_COLUMN)
    assert kinds.index(ChangeKind.DROP_COLUMN) < len(kinds)


def test_identifiers_are_quoted() -> None:
    """Always quoting removes any question of injection through a table name, and makes
    mixed-case and reserved-word identifiers work without a special case."""
    live = {"tables": []}
    target = {"tables": {'weird"name': {"columns": {"select": {"type": "text"}}}}}
    sql = diff_schema(live, target).changes[0].sql
    assert 'CREATE TABLE "weird""name"' in sql
    assert '"select" text' in sql


def test_unsupported_target_key_is_refused() -> None:
    """A migration tool that silently ignores part of your intent is worse than one
    that admits it doesn't handle it."""
    with pytest.raises(PgopsError) as exc_info:
        diff_schema(LIVE, {"tables": {}, "views": {"v": "SELECT 1"}})
    assert exc_info.value.code is ErrorCode.INVALID_ARGUMENT


def test_unsupported_table_key_is_refused() -> None:
    with pytest.raises(PgopsError):
        diff_schema(LIVE, {"tables": {"orders": {"triggers": {"t": "..."}}}})


def test_column_without_type_is_refused() -> None:
    with pytest.raises(PgopsError):
        diff_schema(LIVE, {"tables": {"orders": {"columns": {"x": {"nullable": True}}}}})


def test_ledger_table_is_never_dropped() -> None:
    """Regression: with allow_drops=True and a target that (reasonably) does not mention
    it, the diff emitted `DROP TABLE pgops_migrations` — the engine would destroy the
    ledger recording the migration it was running, then fail writing the result.
    Caught exactly that way, by an apply crashing with
    `relation "pgops_migrations" does not exist`."""
    live = {
        "tables": [
            {"name": "orders", "columns": [], "constraints": [], "indexes": []},
            {"name": "pgops_migrations", "columns": [], "constraints": [], "indexes": []},
        ]
    }
    target = {"tables": {"orders": {"columns": {"id": {"type": "bigint"}}}}}
    result = diff_schema(live, target, allow_drops=True)
    assert not any("pgops_migrations" in c.sql for c in result.changes)


def test_ledger_table_cannot_be_targeted() -> None:
    with pytest.raises(PgopsError) as exc_info:
        diff_schema(LIVE, {"tables": {"pgops_migrations": {"columns": {"x": {"type": "text"}}}}})
    assert exc_info.value.code is ErrorCode.INVALID_ARGUMENT


def test_missing_tables_key_is_refused() -> None:
    with pytest.raises(PgopsError) as exc_info:
        diff_schema(LIVE, {"orders": {}})
    assert exc_info.value.code is ErrorCode.INVALID_ARGUMENT


# --- Statement smuggling ---------------------------------------------------------
#
# Parts of a target spec are SQL text that pgops interpolates into DDL it generates: a
# column type, a DEFAULT expression, a CHECK definition, an index column list. Before
# these checks existed, a constraint definition of
# "CHECK (id > 0); DROP TABLE customers; --" produced ONE Change whose kind was
# add_constraint, and the planner therefore reported the whole plan as
# `destructive: false, highest_risk: medium` — while migration.apply ran the DROP.
# Verified end-to-end against a live database before the fix: 600k rows, one step,
# "applied: true".


@pytest.mark.parametrize(
    ("label", "target"),
    [
        (
            "constraint definition",
            {"tables": {"orders": {"constraints": {"c": "CHECK (id > 0); DROP TABLE t"}}}},
        ),
        (
            "index columns",
            {"tables": {"orders": {"indexes": {"i": "id); DROP TABLE t; CREATE INDEX z ON o (id"}}}},
        ),
        (
            "column type",
            {"tables": {"orders": {"columns": {"n": {"type": "int; DROP TABLE t"}}}}},
        ),
        (
            "column default",
            {"tables": {"orders": {"columns": {"n": {"type": "int", "default": "0; DROP TABLE t"}}}}},
        ),
    ],
)
def test_second_statement_smuggled_into_a_fragment_is_refused(
    label: str, target: dict[str, object]
) -> None:
    with pytest.raises(PgopsError) as exc:
        diff_schema(LIVE, target)
    assert exc.value.code is ErrorCode.INVALID_ARGUMENT
    assert "more than one SQL statement" in exc.value.message


def test_comment_in_a_fragment_is_refused() -> None:
    """A comment can hide the tail of the generated statement from a human reviewer."""
    target = {"tables": {"orders": {"constraints": {"c": "CHECK (id > 0) -- rest hidden"}}}}
    with pytest.raises(PgopsError) as exc:
        diff_schema(LIVE, target)
    assert "comment" in exc.value.message


def test_semicolon_inside_a_string_literal_is_still_allowed() -> None:
    """The check must not be a naive scan for ';' — this default is legitimate SQL."""
    target = {"tables": {"orders": {"columns": {"sep": {"type": "text", "default": "';'"}}}}}
    changes = diff_schema(LIVE, target).changes
    assert changes[0].sql == 'ALTER TABLE "orders" ADD COLUMN "sep" text DEFAULT \';\''


def test_one_statement_invariant_holds_at_change_construction() -> None:
    """The backstop, independent of any field-level check.

    Field checks can only cover the fields known today; this makes a multi-statement
    Change unrepresentable no matter which future code path builds it.
    """
    with pytest.raises(PgopsError) as exc:
        Change(kind=ChangeKind.CREATE_INDEX, table="t", sql="CREATE INDEX a ON t (x); DROP TABLE t")
    assert "more than one SQL statement" in exc.value.message


def test_index_spec_as_a_dict_is_refused_with_a_useful_message() -> None:
    """{"columns": [...]} is the shape an agent reaches for first.

    It used to join the dict's *keys*, planning `CREATE INDEX ... (columns)`, and the
    mistake only surfaced as `column "columns" does not exist` from the database.
    """
    target = {"tables": {"orders": {"indexes": {"i": {"columns": ["a", "b"]}}}}}
    with pytest.raises(PgopsError) as exc:
        diff_schema(LIVE, target)
    assert "list of column names" in exc.value.message


def test_unsupported_column_key_names_the_supported_ones() -> None:
    """primary_key is the key agents try; the error must point at where it belongs."""
    target = {"tables": {"s": {"columns": {"id": {"type": "bigserial", "primary_key": True}}}}}
    with pytest.raises(PgopsError) as exc:
        diff_schema(LIVE, target)
    assert exc.value.hint is not None
    assert "constraints" in exc.value.hint


@pytest.mark.parametrize(
    "target",
    [
        {"tables": {"orders; DROP TABLE api_keys; --": {"columns": {"id": {"type": "int"}}}}},
        {"tables": {"users -- DROP TABLE api_keys": {"columns": {"id": {"type": "int"}}}}},
        {"tables": {"t": {"columns": {"id); DROP TABLE api_keys; --": {"type": "int"}}}}},
    ],
)
def test_identifier_carrying_a_statement_is_refused(target: dict[str, object]) -> None:
    """Table and column names are quoted, so they cannot inject — but a rogue agent used
    exactly these to litter the catalog with tables named after DROP statements. Every
    other target field refuses a statement separator; the identifier must too."""
    with pytest.raises(PgopsError) as exc:
        diff_schema(LIVE, target)
    assert exc.value.code is ErrorCode.INVALID_ARGUMENT


@pytest.mark.parametrize(
    "target",
    [
        {"tables": {"t": {"columns": {"c": {"type": {"nested": "junk"}}}}}},
        {"tables": {"t": {"columns": {"c": {"type": ""}}}}},
        {"tables": {"t": {"columns": {"c": {"type": "int", "default": {"x": 1}}}}}},
        {"tables": {"t": {"columns": {"c": {"type": "int", "nullable": ["y"]}}}}},
    ],
)
def test_non_scalar_column_fields_are_refused_not_crashed(target: dict[str, object]) -> None:
    """A non-string type (or a dict default/nullable) used to pass validation and then
    crash _column_definition with a TypeError -> opaque INTERNAL_ERROR. Found by fuzzing
    the migration target."""
    with pytest.raises(PgopsError) as exc:
        diff_schema(LIVE, target)
    assert exc.value.code is ErrorCode.INVALID_ARGUMENT
