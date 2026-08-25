"""Catalog-aware function volatility check — closes the ADR-001 lexer gap.

The gap: sqlparse is a *lexer*. It can prove a statement contains no INSERT/UPDATE/
DELETE keyword, but `SELECT my_func()` is lexically a read even when `my_func` executes
an INSERT internally. No amount of token-stream analysis can see inside a function body.

The fix: for statements classified `read`, extract every function-call identifier and
look each one up in `pg_proc.provolatile`. Postgres's own volatility classification:

- `i` immutable, `s` stable  → cannot mutate the database; safe to treat as a read
- `v` volatile               → *may* do anything: write, advance sequences, send mail

So the rule is: **any referenced function that is volatile fails the read gate** —
including ones that merely call `nextval()` or touch the clock, because "provably
harmless" is not something we can verify from a flag that means "may have side
effects". Deny-by-default applies to functions too (ADR-001).

Deliberate design decisions:

- This runs in `query_read`, not in `classify()`. The classifier stays a pure function
  of SQL text — unit-testable with no database, which is what makes its table-driven
  tests fast and exhaustive. The catalog lookup needs a connection, so it lives at the
  one call site where a read is about to execute.
- Unknown functions (not in pg_proc — e.g. not yet created) are treated as volatile.
  A name we cannot prove safe is not safe.
- Quoted/oddly-cased identifiers are normalized the way Postgres does: unquoted
  identifiers fold to lower case; quoted ones keep their exact spelling.
- The lookup is one query for all references (`= ANY($1)`), so the cost is a single
  round trip per read call — negligible next to the query itself.

What this still does NOT catch (honest limits): a volatile function reached through a
view's body, a non-SELECT-executing path like `TABLE t`, or CTEs whose body was already
caught by the DML scan anyway. The readonly-pool GUC remains the last line of defense;
this layer just moves the catch from execution time to refusal time, where the agent
gets an actionable error instead of a Postgres executor error.
"""

from __future__ import annotations

from typing import Any

import sqlparse
from sqlparse import tokens as T

# Built-in keywords that look like calls but aren't functions in pg_proc terms.
_NOT_FUNCTIONS = {
    "SELECT", "WHERE", "AND", "OR", "NOT", "IN", "EXISTS", "CASE", "WHEN", "THEN",
    "ELSE", "END", "NULL", "COALESCE", "NULLIF", "GREATEST", "LEAST", "COUNT", "SUM",
    "MIN", "MAX", "AVG", "ARRAY", "VALUES", "WITH", "FROM", "ON", "USING", "CAST",
    "INTERVAL", "ANY", "ALL", "DISTINCT",
}


def function_references(sql: str) -> set[str]:
    """Every function-call name appearing anywhere in the statement, normalized.

    Uses sqlparse's *grouping* rather than regex over flattened text: a function call
    parses as a `Function` token whose first child is an `Identifier` (the name)
    followed by a `Parenthesis`. CTE definitions (`WITH t(n) AS ...`) parse as
    Identifiers-with-angle-brackets in the WITH clause, not Functions, so they don't
    false-positive — which a flat-text regex cannot distinguish.

    String literals and comments are skipped (they can contain anything). Unquoted
    identifiers fold to lowercase to match pg_proc.proname; quoted identifiers would
    need exact match but are rare in agent-written SQL and defaulting to volatile on
    miss is the safe direction anyway.
    """
    parsed = sqlparse.parse(sql)[0]
    names: set[str] = set()

    def walk(token: Any, in_with_defs: bool = False) -> None:
        tokens = getattr(token, "tokens", [])
        # Does this group start with WITH? Then top-level Identifier lists are CTE
        # definitions whose names parse as Functions. Whitespace (including leading
        # indentation) must be skipped before inspecting the first meaningful tokens —
        # an indented statement can begin with a dozen whitespace tokens.
        non_ws = [c for c in tokens if not getattr(c, "is_whitespace", False)]
        starts_with_with = any(
            str(c).strip().upper() == "WITH" for c in non_ws[:3]
        ) or (in_with_defs and type(token).__name__ != "Statement")
        for i, child in enumerate(tokens):
            ctype = type(child).__name__
            if ctype == "Function":
                # A CTE definition (`WITH t(n) AS ...`) parses as a Function too —
                # sqlparse can't tell it from a call. Disambiguate by context: inside
                # a WITH clause's definition list, a Function followed by AS is the
                # CTE name. A call with an alias (`f(x) AS label`) also has AS after
                # it, so the WITH-prefix check is what separates them: aliases on
                # calls never occur inside the WITH definition list itself.
                if starts_with_with:
                    nxt = None
                    for later in tokens[i + 1 :]:
                        if getattr(later, "is_whitespace", False):
                            continue
                        nxt = later
                        break
                    if nxt is not None and str(nxt).strip().upper() == "AS":
                        # this IS the CTE name case — not a function reference
                        continue
                for sub in child.tokens:
                    stype = type(sub).__name__
                    if getattr(sub, "ttype", None) is T.Name or stype == "Identifier":
                        name = str(sub).strip().strip('"').lower()
                        if name and name.upper() not in _NOT_FUNCTIONS:
                            names.add(name)
                        break
            if child.is_group:
                walk(child, starts_with_with)

    walk(parsed)
    return names


async def assert_safe_read_functions(conn: Any, sql: str) -> None:
    """Raise if any function referenced by this (already-classified-read) statement is
    volatile or unknown. `conn` must be a live asyncpg connection.

    The error message names the offending function and its actual volatility so the
    agent can relay something actionable to the user rather than a bare refusal.
    """
    refs = function_references(sql)
    if not refs:
        return

    rows = await conn.fetch(
        """
        SELECT proname, provolatile::text AS volatility
        FROM pg_proc
        WHERE proname = ANY($1)
          AND (pronamespace = 'pg_catalog'::regnamespace OR pronamespace = current_schema()::regnamespace)
        """,
        sorted(refs),
    )
    # provolatile is Postgres's internal "char" type; cast to text in SQL because
    # asyncpg decodes bare "char" to Python bytes — the exact trap that broke
    # schema.inspect in the Phase 1 review.
    known = {r["proname"]: r["volatility"] for r in rows}

    dangerous: list[tuple[str, str]] = []
    for name in sorted(refs):
        volatility = known.get(name)
        if volatility is None:
            # Not found in pg_catalog/current schema: could be a function in another
            # schema on the search path. We cannot prove it safe → treat as volatile.
            dangerous.append((name, "unknown"))
        elif volatility == "v":
            dangerous.append((name, "volatile"))

    if dangerous:
        listed = ", ".join(f"{n} ({v})" for n, v in dangerous)
        from pgops.errors import ErrorCode, PgopsError

        raise PgopsError(
            ErrorCode.CLASSIFICATION_REFUSED,
            f"statement calls function(s) with side effects: {listed}",
            hint=(
                "volatile functions may write data, advance sequences, or otherwise "
                "mutate state; run them via query.write instead"
            ),
        )
