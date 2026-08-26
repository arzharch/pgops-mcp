"""Volatile-function detection — the ADR-001 lexer gap, closed.

`classify()` is a pure function of SQL text and cannot see inside a function body:
`SELECT sneaky_write()` is lexically a read even when the function executes an INSERT.
This module adds the catalog-aware second pass (`function_safety.py`) that runs at
query time against `pg_proc.provolatile`.

Tests here are split:
- pure extraction tests (no DB): what counts as a function reference, what doesn't
- live tests against real Postgres: the actual provolatile lookup, including a real
  write-inside-a-function scenario proving the refusal fires before execution.

ADR-005 applies: the live half runs against real Postgres via testcontainers, because
the whole point is Postgres's own volatility classification, not our guess at it.
"""

from __future__ import annotations

import pytest

from pgops.errors import ErrorCode, PgopsError
from pgops.function_safety import assert_safe_read_functions, function_references

# --- pure extraction (no DB) ----------------------------------------------------------


def test_extracts_simple_call() -> None:
    assert function_references("SELECT my_func()") == {"my_func"}


def test_extracts_schema_qualified_call() -> None:
    # pg_proc stores bare names; the qualifier is dropped
    assert function_references("SELECT pg_catalog.nextval('s')") == {"nextval"}


def test_extracts_calls_inside_cte_and_subquery() -> None:
    refs = function_references("WITH x AS (SELECT do_things()) SELECT * FROM (SELECT other_fn()) y")
    assert {"do_things", "other_fn"} <= refs


def test_string_literals_are_not_function_references() -> None:
    assert function_references("SELECT 'insert into t values (1)' AS x") == set()


def test_comments_are_not_function_references() -> None:
    assert function_references("SELECT 1 /* evil_fn() */ FROM t") == set()


def test_keywords_are_not_function_references() -> None:
    # COUNT/SUM etc. are aggregates, not pg_proc volatility concerns; EXISTS/CASE are
    # keywords that happen to precede parens
    refs = function_references("SELECT count(*), CASE WHEN exists(SELECT 1) THEN 1 END")
    assert "count" not in refs
    assert "case" not in refs
    assert "exists" not in refs


def test_identifiers_fold_to_lowercase() -> None:
    assert function_references("SELECT MyFunc()") == {"myfunc"}


# --- live catalog behavior (real Postgres) ---------------------------------------------


@pytest.fixture
async def volatile_conn(dsn: str):
    """A connection plus a real volatile function that writes when called.

    Creates the `items` table itself (rather than requesting conn_manager) because
    these tests need a raw connection for the catalog lookups and don't want to pay
    for the full ConnectionManager fixture.
    """
    import asyncpg

    conn = await asyncpg.connect(dsn)
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS items (
            id serial PRIMARY KEY,
            name text NOT NULL
        )
        """
    )
    await conn.execute(
        """
        CREATE OR REPLACE FUNCTION test_sneaky_write() RETURNS int AS $$
        BEGIN
            INSERT INTO items (name) VALUES ('injected-by-function');
            RETURN 1;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    try:
        yield conn
    finally:
        await conn.execute("DROP FUNCTION IF EXISTS test_sneaky_write()")
        await conn.execute("DELETE FROM items WHERE name = 'injected-by-function'")
        await conn.close()


async def test_volatile_function_is_refused(volatile_conn) -> None:
    with pytest.raises(PgopsError) as exc_info:
        await assert_safe_read_functions(volatile_conn, "SELECT test_sneaky_write()")
    assert exc_info.value.code is ErrorCode.CLASSIFICATION_REFUSED
    assert "test_sneaky_write" in exc_info.value.message
    assert "volatile" in exc_info.value.message


async def test_volatile_function_refusal_happens_before_execution(volatile_conn, dsn: str) -> None:
    """The actual guarantee: refusing the read means the function never ran."""
    import asyncpg

    with pytest.raises(PgopsError):
        await assert_safe_read_functions(volatile_conn, "SELECT test_sneaky_write()")

    check = await asyncpg.connect(dsn)
    try:
        n = await check.fetchval("SELECT count(*) FROM items WHERE name = 'injected-by-function'")
        assert n == 0, "refused statement must not have executed"
    finally:
        await check.close()


async def test_stable_and_immutable_functions_pass(volatile_conn) -> None:
    # now() is stable; abs() is immutable — both provably non-mutating
    await assert_safe_read_functions(volatile_conn, "SELECT now(), abs(-1)")


async def test_nextval_is_caught(volatile_conn) -> None:
    """nextval() advances sequence state that can't be rolled back — lexically a
    SELECT, volatility-wise exactly what this layer exists to catch."""
    with pytest.raises(PgopsError) as exc_info:
        await assert_safe_read_functions(volatile_conn, "SELECT nextval('items_id_seq')")
    assert "nextval" in exc_info.value.message


async def test_unknown_function_treated_as_volatile(volatile_conn) -> None:
    """A name we can't prove safe is not safe — deny by default (ADR-001)."""
    with pytest.raises(PgopsError) as exc_info:
        await assert_safe_read_functions(volatile_conn, "SELECT definitely_not_created_fn()")
    assert "unknown" in exc_info.value.message


async def test_no_references_is_a_noop(volatile_conn) -> None:
    await assert_safe_read_functions(volatile_conn, "SELECT 1 + 1")


# --- end-to-end through query.read ------------------------------------------------------


async def test_query_read_refuses_volatile_function(conn_manager, config, volatile_conn) -> None:
    """The full path an agent hits: classify passes (lexically a read), then the
    catalog check refuses — with an actionable message, before anything executes."""
    from pgops.tools.query import query_read

    with pytest.raises(PgopsError) as exc_info:
        await query_read(conn_manager, config, "SELECT test_sneaky_write()")
    assert exc_info.value.code is ErrorCode.CLASSIFICATION_REFUSED
    assert "volatile" in exc_info.value.message


async def test_query_read_still_allows_normal_reads(conn_manager, config) -> None:
    from pgops.tools.query import query_read

    result = await query_read(conn_manager, config, "SELECT count(*) AS n FROM items")
    assert result.rows[0]["n"] >= 0
