"""Table-driven classifier cases (ADR-001). No DB needed — pure SQL-text classification."""

from __future__ import annotations

import pytest

from pgops.classifier import StatementClass, classify

CASES: list[tuple[str, StatementClass]] = [
    ("SELECT * FROM orders", StatementClass.READ),
    ("select * from orders where id = 1", StatementClass.READ),
    ("WITH x AS (SELECT 1) SELECT * FROM x", StatementClass.READ),
    ("EXPLAIN SELECT * FROM orders", StatementClass.READ),
    ("EXPLAIN (ANALYZE) SELECT * FROM orders", StatementClass.READ),
    ("TABLE orders", StatementClass.READ),
    # string literals containing DML-looking words must NOT trip the write detector
    ("SELECT 'insert' AS label, 'delete' AS other", StatementClass.READ),
    ("INSERT INTO orders (customer_id) VALUES (1)", StatementClass.WRITE),
    ("UPDATE orders SET status = 'paid' WHERE id = 1", StatementClass.WRITE),
    ("DELETE FROM orders WHERE id = 1", StatementClass.WRITE),
    ("EXPLAIN INSERT INTO orders (customer_id) VALUES (1)", StatementClass.WRITE),
    # write hiding inside a CTE behind an outer SELECT — the case ADR-001 calls out
    (
        "WITH x AS (INSERT INTO orders (customer_id) VALUES (1) RETURNING *) SELECT * FROM x",
        StatementClass.WRITE,
    ),
    (
        "WITH x AS (DELETE FROM orders WHERE id = 1 RETURNING *) SELECT * FROM x",
        StatementClass.WRITE,
    ),
    ("CREATE TABLE t (id int)", StatementClass.DDL),
    ("ALTER TABLE orders ADD COLUMN foo int", StatementClass.DDL),
    ("CREATE INDEX CONCURRENTLY idx ON orders (status)", StatementClass.DDL),
    ("DROP TABLE orders", StatementClass.DESTRUCTIVE),
    ("DROP INDEX idx_orders_customer_id", StatementClass.DESTRUCTIVE),
    ("TRUNCATE orders", StatementClass.DESTRUCTIVE),
    ("ALTER TABLE orders DROP COLUMN status", StatementClass.DESTRUCTIVE),
    # ALTER subcommands that remove a guarantee. A rogue write-token agent dropped a
    # table's primary key and disabled its triggers with these, unconfirmed, because they
    # classified as plain DDL — found against the live 0.1.3 server.
    ("ALTER TABLE api_keys DROP CONSTRAINT IF EXISTS api_keys_pkey", StatementClass.DESTRUCTIVE),
    ("ALTER TABLE api_keys DISABLE TRIGGER ALL", StatementClass.DESTRUCTIVE),
    ("ALTER TABLE orders DISABLE ROW LEVEL SECURITY", StatementClass.DESTRUCTIVE),
    ("ALTER TABLE orders NO FORCE ROW LEVEL SECURITY", StatementClass.DESTRUCTIVE),
    # …but re-enabling a protection, or an ordinary column add, is not destructive.
    ("ALTER TABLE orders ENABLE TRIGGER ALL", StatementClass.DDL),
    ("ALTER TABLE orders ALTER COLUMN status SET DEFAULT 'new'", StatementClass.DDL),
    ("SELECT 1; SELECT 2", StatementClass.UNKNOWN),
    ("SELECT 1; DROP TABLE orders", StatementClass.UNKNOWN),
    ("DO $$ BEGIN NULL; END $$", StatementClass.UNKNOWN),
    ("VACUUM orders", StatementClass.UNKNOWN),
    ("COPY orders TO STDOUT", StatementClass.UNKNOWN),
    ("", StatementClass.UNKNOWN),
]


@pytest.mark.parametrize("sql, expected", CASES)
def test_classify(sql: str, expected: StatementClass) -> None:
    result = classify(sql)
    assert result.kind is expected, (
        f"{sql!r} -> {result.kind} ({result.reason}), expected {expected}"
    )


def test_unknown_gates_as_destructive() -> None:
    result = classify("VACUUM orders")
    assert result.kind is StatementClass.UNKNOWN
    assert result.effective_gate_class is StatementClass.DESTRUCTIVE


def test_read_gate_helper() -> None:
    assert classify("SELECT 1").is_read
    assert not classify("INSERT INTO t VALUES (1)").is_read
    assert not classify("VACUUM t").is_read


def test_unparseable_statement_fails_closed_not_crash() -> None:
    """sqlparse caps a statement at 10,000 tokens and raises past that. classify() is the
    safety gate, so an unparseable statement must return UNKNOWN (which the guardrail
    refuses), never escape as an opaque INTERNAL_ERROR. Found by fuzzing a 20,000-element
    IN list against the live server."""
    huge = "SELECT 1 WHERE x IN (" + ",".join("'a'" for _ in range(20000)) + ")"
    c = classify(huge)
    assert c.kind is StatementClass.UNKNOWN
    assert "could not be parsed" in c.reason
