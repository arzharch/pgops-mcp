"""SQL statement classifier — deny-by-default (ADR-001).

Design: allowlist, not blocklist. We don't scan for "bad" patterns and let everything
else through; we require positive proof a statement is a pure read, and anything that
doesn't earn that proof falls through to the most dangerous applicable class.

Why sqlparse over a real parser (pglast/libpg_query) or hand-rolled regex:
- pglast wraps libpg_query (C extension via the actual Postgres grammar) and would be
  the most *correct* choice — it can't be fooled by anything sqlparse's lexer misreads.
  Rejected for v1 because it ships prebuilt C extensions per platform/Python version;
  that directly fights G5 (installable via uv/pipx in <2 min, works everywhere). A
  classifier that's 100% correct but only installs on Linux isn't safer in practice.
- Hand-rolled regex over raw SQL text is the wrong shape for this problem: string
  literals can contain the word "insert" or "drop", comments can hide anything, and
  regex has no concept of nesting (CTEs). It would need to reimplement a lexer anyway.
- sqlparse is a pure-Python SQL *lexer* (not a semantic parser — it doesn't understand
  schemas or types). That's exactly the boundary we need: it tokenizes reliably enough
  to (a) tell a string literal from a keyword and (b) find every DML keyword token in a
  statement no matter how deeply it's nested inside a CTE, without pulling in a platform
  dependency. What it can't do — know that `SELECT my_func()` calls a volatile function
  that writes underneath — is a real, named gap (ADR-001), and it's why query.read runs
  on a connection with `default_transaction_read_only = on` (connections.py): even a
  classifier that's fooled still can't get a write through, because Postgres itself
  refuses it at the executor level. Defense in depth, not classifier-as-only-guard.

Multi-statement submissions (`sql; sql2`) are rejected outright regardless of what
either half is — stacked queries are a classic injection shape and there's no legitimate
reason an agent needs to submit two statements in one call.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import sqlparse
from sqlparse import tokens as T
from sqlparse.sql import Token

_DESTRUCTIVE_LEADING = {"DROP", "TRUNCATE"}
_DDL_LEADING = {"CREATE", "ALTER", "COMMENT"}
_READ_LEADING = {"SELECT", "WITH", "EXPLAIN", "TABLE", "VALUES"}
_WRITE_DML = {"INSERT", "UPDATE", "DELETE"}


class StatementClass(StrEnum):
    READ = "read"
    WRITE = "write"
    DDL = "ddl"
    DESTRUCTIVE = "destructive"
    UNKNOWN = "unknown"


@dataclass(slots=True, frozen=True)
class Classification:
    kind: StatementClass
    leading_keyword: str
    reason: str

    @property
    def is_read(self) -> bool:
        return self.kind is StatementClass.READ

    @property
    def effective_gate_class(self) -> StatementClass:
        """What guardrails should actually treat this as. UNKNOWN is reported as its
        own kind for audit/debugging visibility, but is never *less* dangerous than
        DESTRUCTIVE when deciding whether to allow execution (ADR-001)."""
        return StatementClass.DESTRUCTIVE if self.kind is StatementClass.UNKNOWN else self.kind


def classify(sql: str) -> Classification:
    statements = [s for s in sqlparse.split(sql) if s.strip()]
    if len(statements) == 0:
        return Classification(StatementClass.UNKNOWN, "", "empty statement")
    if len(statements) > 1:
        return Classification(
            StatementClass.UNKNOWN,
            "MULTI",
            f"submission contains {len(statements)} statements; only one is allowed per call",
        )

    parsed = sqlparse.parse(statements[0])[0]
    tokens: list[Token] = [
        tok
        for tok in parsed.flatten()  # type: ignore[no-untyped-call]
        if not tok.is_whitespace and tok.ttype not in T.Comment
    ]
    if not tokens:
        return Classification(StatementClass.UNKNOWN, "", "empty statement")

    leading = _leading_keyword(tokens)

    # A write DML keyword ANYWHERE in the token stream — including inside a CTE body
    # like `WITH x AS (INSERT INTO ... RETURNING ...) SELECT * FROM x` — makes this a
    # write, no matter what the outer statement looks like. sqlparse tags these as
    # Token.Keyword.DML distinctly from string/identifier tokens, so
    # `SELECT 'insert' AS label` does NOT trip this (checked in test_classifier.py).
    write_hit = any(
        tok.ttype is T.Keyword.DML and tok.normalized.upper() in _WRITE_DML for tok in tokens
    )
    if write_hit:
        return Classification(StatementClass.WRITE, leading, "contains INSERT/UPDATE/DELETE")

    if _is_drop_column(tokens):
        return Classification(StatementClass.DESTRUCTIVE, leading, "ALTER ... DROP COLUMN")

    if leading in _DESTRUCTIVE_LEADING:
        return Classification(StatementClass.DESTRUCTIVE, leading, f"{leading} statement")

    if leading in _DDL_LEADING:
        return Classification(StatementClass.DDL, leading, f"{leading} statement")

    if leading in _READ_LEADING:
        return Classification(
            StatementClass.READ, leading, f"{leading} statement, no write DML found"
        )

    return Classification(
        StatementClass.UNKNOWN, leading, f"unrecognized leading keyword {leading!r}"
    )


def _leading_keyword(tokens: list[Token]) -> str:
    first = tokens[0]
    word: str = first.normalized.upper()
    if word == "EXPLAIN":
        # classification still runs over the *whole* token stream (write_hit above),
        # so `EXPLAIN INSERT INTO ...` is correctly caught as a write, not a read.
        return "EXPLAIN"
    return word


def _is_drop_column(tokens: list[Token]) -> bool:
    for i, tok in enumerate(tokens[:-1]):
        if tok.ttype in T.Keyword and tok.normalized.upper() == "DROP":
            nxt = tokens[i + 1]
            if nxt.ttype in T.Keyword and nxt.normalized.upper() == "COLUMN":
                return True
    return False
