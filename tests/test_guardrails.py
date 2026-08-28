"""Guardrail unit tests: WHERE detection and the confirmation-token protocol.

SPEC Phase 2 gate: "proves every guardrail blocks what it claims".
"""

from __future__ import annotations

import time

import pytest

from pgops.classifier import classify
from pgops.errors import ErrorCode, PgopsError
from pgops.guardrails import (
    ConfirmationTokenStore,
    RiskReason,
    evaluate,
    has_where_clause,
)


@pytest.mark.parametrize(
    "sql, expected",
    [
        ("DELETE FROM orders WHERE id = 1", True),
        ("UPDATE orders SET status = 'x' WHERE id = 1", True),
        ("DELETE FROM orders", False),
        ("UPDATE orders SET status = 'x'", False),
        # the cases a substring search for "where" gets wrong:
        ("INSERT INTO log (msg) VALUES ('where')", False),
        ("UPDATE orders SET note = 'somewhere'", False),
        ("DELETE FROM orders -- WHERE id = 1", False),
        ("UPDATE orders SET wherefore = 1", False),
        # ...and the case it gets right only by accident:
        ("DELETE FROM orders WHERE created_at < now()", True),
    ],
)
def test_where_detection(sql: str, expected: bool) -> None:
    assert has_where_clause(sql) is expected


def test_bounded_delete_is_allowed() -> None:
    sql = "DELETE FROM orders WHERE id = 1"
    verdict = evaluate(classify(sql), sql)
    assert verdict.allowed is True


def test_unbounded_delete_is_blocked() -> None:
    sql = "DELETE FROM orders"
    verdict = evaluate(classify(sql), sql)
    assert verdict.allowed is False
    assert verdict.risk is RiskReason.UNBOUNDED_MUTATION
    assert "every row" in verdict.reason


def test_unbounded_update_is_blocked() -> None:
    sql = "UPDATE orders SET status = 'cancelled'"
    verdict = evaluate(classify(sql), sql)
    assert verdict.allowed is False
    assert verdict.risk is RiskReason.UNBOUNDED_MUTATION


def test_insert_needs_no_where_clause() -> None:
    """INSERT has no WHERE by design — the unbounded check must not fire on it."""
    sql = "INSERT INTO orders (customer_id) VALUES (1)"
    verdict = evaluate(classify(sql), sql)
    assert verdict.allowed is True


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE orders",
        "TRUNCATE orders",
        "ALTER TABLE orders DROP COLUMN status",
        # Guarantee-removing ALTER subcommands. A write-token agent ran the first two
        # unconfirmed against the live server before these gated: it dropped a primary
        # key and disabled all triggers on the credentials table.
        "ALTER TABLE api_keys DROP CONSTRAINT api_keys_pkey",
        "ALTER TABLE api_keys DISABLE TRIGGER ALL",
        "ALTER TABLE orders DISABLE ROW LEVEL SECURITY",
    ],
)
def test_destructive_statements_blocked(sql: str) -> None:
    verdict = evaluate(classify(sql), sql)
    assert verdict.allowed is False
    assert verdict.risk is RiskReason.DESTRUCTIVE_STATEMENT
    assert verdict.requires_confirmation is True


@pytest.mark.parametrize("sql", ["VACUUM orders", "DO $$ BEGIN NULL; END $$", "SELECT 1; SELECT 2"])
def test_unclassifiable_statements_blocked(sql: str) -> None:
    verdict = evaluate(classify(sql), sql)
    assert verdict.allowed is False
    assert verdict.risk is RiskReason.UNKNOWN_STATEMENT


def test_token_roundtrip() -> None:
    store = ConfirmationTokenStore()
    sql = "DROP TABLE orders"
    token = store.issue(sql, "destructive")
    store.redeem(token, sql)  # no exception == approved


def test_token_is_single_use() -> None:
    store = ConfirmationTokenStore()
    sql = "DROP TABLE orders"
    token = store.issue(sql, "destructive")
    store.redeem(token, sql)
    with pytest.raises(PgopsError) as exc_info:
        store.redeem(token, sql)
    assert exc_info.value.code is ErrorCode.INVALID_CONFIRMATION


def test_token_bound_to_exact_statement() -> None:
    """The attack this prevents: get approval for a harmless statement, then redeem
    that approval against a destructive one."""
    store = ConfirmationTokenStore()
    token = store.issue("DELETE FROM staging_data", "destructive")
    with pytest.raises(PgopsError) as exc_info:
        store.redeem(token, "DELETE FROM orders")
    assert exc_info.value.code is ErrorCode.CONFIRMATION_MISMATCH


def test_mismatched_redeem_does_not_burn_the_token() -> None:
    """A mismatch means this call was not the approved one — the user's actual pending
    approval must survive it."""
    store = ConfirmationTokenStore()
    sql = "DELETE FROM staging_data"
    token = store.issue(sql, "destructive")
    with pytest.raises(PgopsError):
        store.redeem(token, "DELETE FROM orders")
    store.redeem(token, sql)  # still valid for what it was issued for


def test_unknown_token_rejected() -> None:
    store = ConfirmationTokenStore()
    with pytest.raises(PgopsError) as exc_info:
        store.redeem("not-a-real-token", "DROP TABLE orders")
    assert exc_info.value.code is ErrorCode.INVALID_CONFIRMATION


def test_token_expires() -> None:
    store = ConfirmationTokenStore(ttl_s=0)
    sql = "DROP TABLE orders"
    token = store.issue(sql, "destructive")
    time.sleep(0.01)
    with pytest.raises(PgopsError) as exc_info:
        store.redeem(token, sql)
    assert exc_info.value.code is ErrorCode.INVALID_CONFIRMATION


def test_tokens_are_unpredictable() -> None:
    store = ConfirmationTokenStore()
    issued = {store.issue("DROP TABLE orders", "destructive") for _ in range(100)}
    assert len(issued) == 100
    assert all(len(t) >= 24 for t in issued)
