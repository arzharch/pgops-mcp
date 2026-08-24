"""Elicitation-based approval, and the token protocol as fallback.

The point of these tests is the *degradation policy*: when elicitation is unavailable,
approval must weaken to the token flow — never to "allowed".
"""

from __future__ import annotations

from typing import Any

import asyncpg
import pytest

from pgops.approval import ApprovalMethod, client_log, report_progress, request_approval
from pgops.audit import AuditLog
from pgops.config import PgopsConfig
from pgops.connections import ConnectionManager
from pgops.errors import ErrorCode, PgopsError
from pgops.guardrails import ConfirmationTokenStore
from pgops.tools.write import query_write


class FakeContext:
    """Stands in for a FastMCP Context. `behaviour` decides what the client does."""

    def __init__(self, behaviour: str, choice: str = "approve") -> None:
        self.behaviour = behaviour
        self.choice = choice
        self.elicited: list[str] = []
        self.response_types: list[Any] = []
        self.progress: list[tuple[float, float, str]] = []
        self.logs: list[tuple[str, str]] = []

    async def elicit(self, message: str, response_type: Any = None, **kwargs: Any) -> Any:
        self.elicited.append(message)
        self.response_types.append(response_type)
        if self.behaviour == "unsupported":
            raise RuntimeError("client does not support elicitation")

        outer = self

        class _Result:
            action = outer.behaviour
            data = outer.choice

        return _Result()

    async def report_progress(self, progress: float, total: float, message: str) -> None:
        self.progress.append((progress, total, message))

    async def log(self, message: str, level: str = "info") -> None:
        self.logs.append((level, message))


async def _count(dsn: str) -> int:
    conn = await asyncpg.connect(dsn)
    try:
        return int(await conn.fetchval("SELECT count(*) FROM items"))
    finally:
        await conn.close()


# --- request_approval -----------------------------------------------------------------


async def test_accept_is_an_approval() -> None:
    result = await request_approval(FakeContext("accept"), "do it", "because")
    assert result.approved is True
    assert result.method is ApprovalMethod.ELICITATION


async def test_decline_is_not_an_approval() -> None:
    result = await request_approval(FakeContext("decline"), "do it", "because")
    assert result.approved is False
    assert result.method is ApprovalMethod.ELICITATION


async def test_cancel_is_not_an_approval() -> None:
    result = await request_approval(FakeContext("cancel"), "do it", "because")
    assert result.approved is False


async def test_unsupported_client_degrades_rather_than_approving() -> None:
    """The critical property: a client that cannot elicit must not be treated as one
    that said yes."""
    result = await request_approval(FakeContext("unsupported"), "do it", "because")
    assert result.approved is False
    assert result.method is ApprovalMethod.UNAVAILABLE


async def test_no_context_is_not_an_approval() -> None:
    result = await request_approval(None, "do it", "because")
    assert result.approved is False
    assert result.method is ApprovalMethod.UNAVAILABLE


async def test_accepting_the_prompt_but_choosing_cancel_is_a_refusal() -> None:
    """The elicitation envelope says the user engaged with the prompt; the *choice*
    says what they decided. Treating `action == "accept"` as the answer would approve
    every action the user actively declined."""
    result = await request_approval(FakeContext("accept", choice="cancel"), "do it", "why")
    assert result.approved is False


async def test_an_explicit_response_type_is_sent() -> None:
    """`response_type=None` yields an empty schema that some clients (VS Code) render
    as an empty, non-functional form — the user is asked to approve something with no
    way to answer. A concrete choice list renders as a real prompt everywhere."""
    ctx = FakeContext("accept")
    await request_approval(ctx, "do it", "why")
    assert ctx.response_types[0] == ["approve", "cancel"]


async def test_the_reason_reaches_the_user() -> None:
    """The whole point of elicitation is that the human sees the real reason rather
    than the model's paraphrase of it."""
    ctx = FakeContext("accept")
    await request_approval(ctx, "Execute: DELETE FROM orders", "affects every row")
    assert "DELETE FROM orders" in ctx.elicited[0]
    assert "affects every row" in ctx.elicited[0]


# --- query.write integration ----------------------------------------------------------


async def test_elicitation_accept_executes_without_a_token(
    conn_manager: ConnectionManager, config: PgopsConfig,
    audit: AuditLog, tokens: ConfirmationTokenStore,
) -> None:
    ctx = FakeContext("accept")
    result = await query_write(
        conn_manager, config, audit, tokens, "DELETE FROM items", ctx=ctx
    )
    assert result.rows_affected == 250
    assert ctx.elicited, "the user should have been asked"


async def test_elicitation_decline_blocks_and_offers_no_token(
    conn_manager: ConnectionManager, config: PgopsConfig,
    audit: AuditLog, tokens: ConfirmationTokenStore,
) -> None:
    """An explicit human "no" must not be convertible into a token the agent can redeem
    a moment later — that would make declining meaningless."""
    ctx = FakeContext("decline")
    with pytest.raises(PgopsError) as exc_info:
        await query_write(conn_manager, config, audit, tokens, "DELETE FROM items", ctx=ctx)

    assert exc_info.value.code is ErrorCode.CONFIRMATION_DECLINED
    assert exc_info.value.hint is None or "confirm_token" not in (exc_info.value.hint or "")
    assert tokens.outstanding() == 0
    assert await _count(config.dsn) == 250


async def test_unsupported_client_falls_back_to_the_token_flow(
    conn_manager: ConnectionManager, config: PgopsConfig,
    audit: AuditLog, tokens: ConfirmationTokenStore,
) -> None:
    """Degraded, but still gated — and still refuses on the first call."""
    ctx = FakeContext("unsupported")
    with pytest.raises(PgopsError) as exc_info:
        await query_write(conn_manager, config, audit, tokens, "DELETE FROM items", ctx=ctx)

    assert exc_info.value.code is ErrorCode.CONFIRMATION_REQUIRED
    assert "confirm_token" in (exc_info.value.hint or "")
    assert await _count(config.dsn) == 250

    token = (exc_info.value.hint or "").split("confirm_token=")[1].split("'")[1]
    result = await query_write(
        conn_manager, config, audit, tokens, "DELETE FROM items", confirm_token=token, ctx=ctx
    )
    assert result.rows_affected == 250


async def test_safe_statement_never_asks_the_user(
    conn_manager: ConnectionManager, config: PgopsConfig,
    audit: AuditLog, tokens: ConfirmationTokenStore,
) -> None:
    """Prompting on every write would train users to click through approvals, which
    destroys the value of prompting on the dangerous ones."""
    ctx = FakeContext("accept")
    await query_write(
        conn_manager, config, audit, tokens, "DELETE FROM items WHERE id <= 5", ctx=ctx
    )
    assert ctx.elicited == []


async def test_audit_records_which_approval_method_was_used(
    conn_manager: ConnectionManager, config: PgopsConfig, tokens: ConfirmationTokenStore
) -> None:
    """"The human was asked directly" and "the agent presented a token" are different
    assurances. An incident review needs to tell them apart."""
    log = AuditLog(config.audit_path)
    await query_write(
        conn_manager, config, log, tokens, "DELETE FROM items", ctx=FakeContext("accept")
    )
    entries = log.read_all()
    assert entries[0]["verdict"] == "approved_by_user"
    assert "elicitation" in entries[0]["detail"]
    assert entries[-1]["verdict"] == "executed"
    assert "approval=elicitation" in entries[-1]["detail"]


async def test_declined_action_is_audited(
    conn_manager: ConnectionManager, config: PgopsConfig, tokens: ConfirmationTokenStore
) -> None:
    log = AuditLog(config.audit_path)
    with pytest.raises(PgopsError):
        await query_write(
            conn_manager, config, log, tokens, "DROP TABLE items", ctx=FakeContext("decline")
        )
    assert log.read_all()[-1]["verdict"] == "declined_by_user"


# --- telemetry helpers ----------------------------------------------------------------


async def test_progress_and_logging_are_best_effort() -> None:
    """Telemetry must never break the operation it describes."""

    class Broken:
        async def report_progress(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("client hung up")

        async def log(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("client hung up")

    await report_progress(Broken(), 1, 10, "step 1")  # must not raise
    await client_log(Broken(), "info", "hello")  # must not raise
    await report_progress(None, 1, 10, "no ctx")
    await client_log(None, "info", "no ctx")


async def test_progress_reaches_the_client() -> None:
    ctx = FakeContext("accept")
    await report_progress(ctx, 2, 5, "applying step 2")
    assert ctx.progress == [(2, 5, "applying step 2")]
