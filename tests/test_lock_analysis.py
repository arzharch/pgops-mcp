"""Lock-impact classification (ADR-004). Pure functions — no database needed.

The behaviours asserted here were verified against Postgres 16 first (relfilenode
before/after each ALTER); these tests lock in what was measured rather than what the
documentation is remembered to say.
"""

from __future__ import annotations

import pytest

from pgops.migrations.lock_analysis import (
    Confidence,
    OperationClass,
    analyze_statement,
)

BIG = 1_000_000


def test_add_column_no_default_is_metadata_only() -> None:
    impact = analyze_statement("ALTER TABLE orders ADD COLUMN note text", BIG)
    assert impact.operation is OperationClass.METADATA_ONLY
    assert impact.rewrites_table is False
    assert impact.risk == "low"


def test_add_column_constant_default_does_not_rewrite() -> None:
    """PG11+ stores a non-volatile default in the catalog — verified: relfilenode
    unchanged after `ADD COLUMN b int NOT NULL DEFAULT 7`."""
    impact = analyze_statement("ALTER TABLE orders ADD COLUMN b int NOT NULL DEFAULT 7", BIG)
    assert impact.operation is OperationClass.METADATA_ONLY
    assert impact.rewrites_table is False


@pytest.mark.parametrize(
    "default_expr",
    ["'pending'", "'pending'::text", "0", "-1", "1.5", "true", "NULL"],
)
def test_constant_defaults_recognised(default_expr: str) -> None:
    impact = analyze_statement(f"ALTER TABLE orders ADD COLUMN c text DEFAULT {default_expr}", BIG)
    assert impact.rewrites_table is False, default_expr


@pytest.mark.parametrize(
    "default_expr",
    ["(random()*10)::int", "gen_random_uuid()", "my_custom_fn()"],
)
def test_volatile_defaults_force_a_rewrite(default_expr: str) -> None:
    """Verified: relfilenode CHANGED after `ADD COLUMN c int DEFAULT (random()*10)::int`.

    This is the distinction that looks like nothing in SQL and costs a full rewrite.
    """
    impact = analyze_statement(f"ALTER TABLE orders ADD COLUMN c int DEFAULT {default_expr}", BIG)
    assert impact.operation is OperationClass.TABLE_REWRITE, default_expr
    assert impact.rewrites_table is True
    assert impact.blocks_reads is True
    assert impact.safe_alternative is not None


def test_alter_column_type_rewrites_and_is_high_risk() -> None:
    impact = analyze_statement("ALTER TABLE orders ALTER COLUMN a TYPE bigint", BIG)
    assert impact.operation is OperationClass.TABLE_REWRITE
    assert impact.blocks_reads is True
    assert impact.risk == "high"
    assert "backfill" in (impact.safe_alternative or "")


def test_alter_type_on_small_table_is_not_high_risk() -> None:
    """Crying wolf about a 100-row table would make the whole risk signal useless."""
    impact = analyze_statement("ALTER TABLE tiny ALTER COLUMN a TYPE bigint", 100)
    assert impact.risk == "low"


def test_create_index_blocks_writes_but_not_reads() -> None:
    impact = analyze_statement("CREATE INDEX idx ON orders (status)", BIG)
    assert impact.operation is OperationClass.INDEX_BUILD
    assert impact.blocks_reads is False
    assert impact.blocks_writes is True
    assert "CONCURRENTLY" in (impact.safe_alternative or "")


def test_create_index_concurrently_blocks_nothing_and_is_not_transactional() -> None:
    """Verified against PG16: `CREATE INDEX CONCURRENTLY cannot run inside a
    transaction block`. Getting this wrong makes an atomic-migration claim false."""
    impact = analyze_statement("CREATE INDEX CONCURRENTLY idx ON orders (status)", BIG)
    assert impact.operation is OperationClass.INDEX_BUILD_CONCURRENT
    assert impact.blocks_reads is False
    assert impact.blocks_writes is False
    assert impact.transactional is False
    assert impact.risk == "low"


def test_concurrent_index_estimated_slower_than_plain() -> None:
    plain = analyze_statement("CREATE INDEX idx ON orders (status)", BIG)
    concurrent = analyze_statement("CREATE INDEX CONCURRENTLY idx ON orders (status)", BIG)
    assert concurrent.estimate_ms is not None and plain.estimate_ms is not None
    assert concurrent.estimate_ms > plain.estimate_ms


def test_set_not_null_scans_but_does_not_rewrite() -> None:
    impact = analyze_statement("ALTER TABLE orders ALTER COLUMN a SET NOT NULL", BIG)
    assert impact.operation is OperationClass.TABLE_SCAN
    assert impact.rewrites_table is False
    assert "NOT VALID" in (impact.safe_alternative or "")


def test_add_constraint_not_valid_is_instant() -> None:
    impact = analyze_statement(
        "ALTER TABLE orders ADD CONSTRAINT c CHECK (total_cents >= 0) NOT VALID", BIG
    )
    assert impact.operation is OperationClass.METADATA_ONLY
    assert impact.blocks_reads is False


def test_validate_constraint_blocks_neither_reads_nor_writes() -> None:
    impact = analyze_statement("ALTER TABLE orders VALIDATE CONSTRAINT c", BIG)
    assert impact.operation is OperationClass.TABLE_SCAN
    assert impact.blocks_reads is False
    assert impact.blocks_writes is False
    assert impact.risk == "low"


def test_add_validated_constraint_suggests_the_two_step_split() -> None:
    impact = analyze_statement("ALTER TABLE orders ADD CONSTRAINT c CHECK (total_cents >= 0)", BIG)
    assert "NOT VALID" in (impact.safe_alternative or "")


def test_drop_column_is_metadata_only_but_still_dangerous() -> None:
    impact = analyze_statement("ALTER TABLE orders DROP COLUMN status", BIG)
    assert impact.operation is OperationClass.METADATA_ONLY
    assert impact.estimate_ms == 1
    # the reasoning must not imply this is harmless
    assert "data loss" in impact.reasoning.lower()


def test_unknown_statement_assumes_the_worst() -> None:
    impact = analyze_statement("CLUSTER orders USING idx_orders_customer_id", BIG)
    assert impact.operation is OperationClass.UNKNOWN
    assert impact.blocks_reads is True
    assert impact.confidence is Confidence.LOW
    assert impact.estimate_ms is None
    assert impact.risk == "unknown"


def test_unknown_alter_variant_assumes_the_worst() -> None:
    impact = analyze_statement("ALTER TABLE orders SET LOGGED", BIG)
    assert impact.operation is OperationClass.UNKNOWN
    assert impact.blocks_reads is True


def test_estimates_scale_with_table_size() -> None:
    small = analyze_statement("ALTER TABLE t ALTER COLUMN a TYPE bigint", 10_000)
    large = analyze_statement("ALTER TABLE t ALTER COLUMN a TYPE bigint", 10_000_000)
    assert small.estimate_ms is not None and large.estimate_ms is not None
    assert large.estimate_ms > small.estimate_ms * 100


def test_estimates_are_never_presented_as_certain() -> None:
    """ADR-004: estimates are honest heuristics. Nothing that scales with table size
    may claim high confidence, because the rate depends on hardware we cannot see."""
    for sql in [
        "ALTER TABLE t ALTER COLUMN a TYPE bigint",
        "CREATE INDEX idx ON t (a)",
        "ALTER TABLE t ALTER COLUMN a SET NOT NULL",
    ]:
        impact = analyze_statement(sql, BIG)
        assert impact.confidence is not Confidence.HIGH, sql
