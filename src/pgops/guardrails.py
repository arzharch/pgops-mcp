"""Guardrails: unbounded-mutation detection and the confirmation-token protocol.

The whole safety argument rests on one idea: an agent must never be able to turn a
single tool call into an irreversible bulk mutation. Two independent mechanisms:

1. **Unbounded-mutation detection.** An UPDATE/DELETE with no WHERE clause affects every
   row. That is almost never what was meant, and it is unrecoverable without a backup.
   Detected structurally from the token stream, not by regex over the SQL text.

2. **Confirmation tokens.** Anything dangerous is refused on first call, with a token
   and a human-readable reason. Executing requires calling again *with* that token. The
   agent cannot mint one, so the user's approval is structurally required rather than
   politely requested.

Token binding is the part that is easy to get wrong. A token that merely says "the user
approved something" is forgeable-by-confusion: an agent could obtain approval for
`DELETE FROM staging` and reuse the token to run `DELETE FROM orders`. So each token is
bound to the SHA-256 of the exact statement it was issued for, and redeeming it against
different SQL fails — even if the token itself is valid and unexpired.

Tokens are also single-use and TTL-bound (default 5 min), held in memory only: a
restarted server invalidates every outstanding approval, which is the safe direction to
fail.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from enum import StrEnum

import sqlparse
from sqlparse import tokens as T
from sqlparse.sql import Token

from pgops.audit import sql_fingerprint
from pgops.classifier import Classification, StatementClass
from pgops.errors import ErrorCode, PgopsError

DEFAULT_TOKEN_TTL_S = 300


class RiskReason(StrEnum):
    UNBOUNDED_MUTATION = "unbounded_mutation"
    DESTRUCTIVE_STATEMENT = "destructive_statement"
    UNKNOWN_STATEMENT = "unknown_statement"


@dataclass(slots=True, frozen=True)
class GuardrailVerdict:
    """`allowed` means execute now. Otherwise `requires_confirmation` says whether a
    token would unblock it — some things are refused outright regardless."""

    allowed: bool
    requires_confirmation: bool
    reason: str
    risk: RiskReason | None = None


def has_where_clause(sql: str) -> bool:
    """True if the statement has a WHERE at the top level of its token stream.

    Uses the token stream rather than `"where" in sql.lower()` because the string test
    matches the word inside a literal (`INSERT INTO log VALUES ('where')`), inside an
    identifier (a column named `wherefore`), and inside a comment — each of which would
    wave through a genuinely unbounded DELETE.
    """
    statements = [s for s in sqlparse.split(sql) if s.strip()]
    if len(statements) != 1:
        return False
    parsed = sqlparse.parse(statements[0])[0]
    tokens: list[Token] = list(parsed.flatten())  # type: ignore[no-untyped-call]
    return any(tok.ttype is T.Keyword and tok.normalized.upper() == "WHERE" for tok in tokens)


def evaluate(classification: Classification, sql: str) -> GuardrailVerdict:
    gate_class = classification.effective_gate_class

    if classification.kind is StatementClass.UNKNOWN:
        return GuardrailVerdict(
            allowed=False,
            requires_confirmation=True,
            reason=(
                f"statement could not be confidently classified ({classification.reason}); "
                "treated as destructive"
            ),
            risk=RiskReason.UNKNOWN_STATEMENT,
        )

    if gate_class is StatementClass.DESTRUCTIVE:
        return GuardrailVerdict(
            allowed=False,
            requires_confirmation=True,
            reason=f"destructive statement ({classification.reason})",
            risk=RiskReason.DESTRUCTIVE_STATEMENT,
        )

    is_bulk_dml = gate_class is StatementClass.WRITE and classification.leading_keyword in {
        "UPDATE",
        "DELETE",
    }
    if is_bulk_dml and not has_where_clause(sql):
        return GuardrailVerdict(
            allowed=False,
            requires_confirmation=True,
            reason=(
                f"{classification.leading_keyword} has no WHERE clause and would "
                "affect every row in the table"
            ),
            risk=RiskReason.UNBOUNDED_MUTATION,
        )

    return GuardrailVerdict(allowed=True, requires_confirmation=False, reason="passed guardrails")


@dataclass(slots=True)
class _Issued:
    sql_hash: str
    expires_at: float
    reason: str


class ConfirmationTokenStore:
    """In-memory, single-use, statement-bound approval tokens.

    Deliberately not persisted. A token outliving the process that issued it would mean
    approvals survive a restart the user never saw — and the failure direction of losing
    a token (one extra confirmation) is far cheaper than the alternative.
    """

    def __init__(self, ttl_s: int = DEFAULT_TOKEN_TTL_S) -> None:
        self._ttl_s = ttl_s
        self._tokens: dict[str, _Issued] = {}

    def issue(self, sql: str, reason: str) -> str:
        # secrets, not random/uuid4: this value is an authorization credential, and the
        # cost of using a CSPRNG here is nil.
        token = secrets.token_urlsafe(24)
        self._tokens[token] = _Issued(
            sql_hash=sql_fingerprint(sql),
            expires_at=time.monotonic() + self._ttl_s,
            reason=reason,
        )
        return token

    def redeem(self, token: str, sql: str) -> None:
        """Consume a token for this exact statement, or raise. Never returns a bool —
        a caller that forgets to check a boolean fails open; one that ignores an
        exception cannot."""
        self._purge_expired()
        issued = self._tokens.get(token)
        if issued is None:
            raise PgopsError(
                ErrorCode.INVALID_CONFIRMATION,
                "confirmation token is invalid, already used, or expired",
                hint="re-run the statement without a token to get a fresh one",
            )
        if issued.sql_hash != sql_fingerprint(sql):
            # Do NOT consume the token here: the statement mismatch means this call was
            # not the one the user approved, so the original approval is still pending
            # and legitimate.
            raise PgopsError(
                ErrorCode.CONFIRMATION_MISMATCH,
                "confirmation token was issued for a different statement",
                hint="tokens are bound to the exact SQL they were issued for",
            )
        # single-use: consume on success
        del self._tokens[token]

    def _purge_expired(self) -> None:
        now = time.monotonic()
        for token in [t for t, issued in self._tokens.items() if issued.expires_at <= now]:
            del self._tokens[token]

    def outstanding(self) -> int:
        self._purge_expired()
        return len(self._tokens)
