"""Schema diff: ordering, type normalization, and the drop-safety default."""

from __future__ import annotations

import pytest

from pgops.errors import ErrorCode, PgopsError
from pgops.migrations.diff import ChangeKind, diff_schema

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
