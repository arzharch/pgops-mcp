"""Per-tool scope enforcement.

Regression test for a real bug: `TOOL_SCOPES` existed and was documented as enforced,
but nothing consulted it. `JWTVerifier(required_scopes=["pgops:read"])` checks scopes
once per request against one server-wide list, so every valid token cleared the only
gate there was. A token issued with `pgops:read` alone was observed running
`CREATE TABLE` successfully before this was fixed.

The distinction under test is authentication (is this caller real) versus authorization
(may this caller do *this*). Passing the first is not passing the second.
"""

from __future__ import annotations

import asyncio

import pytest

from pgops.auth import TOOL_SCOPES, Scope, build_verifier, generate_keypair, issue_token
from pgops.middleware import LOCAL_CALLER, Caller, current_caller


def _caller(*scopes: str) -> Caller:
    return Caller(subject="agent", scopes=frozenset(scopes), authenticated=True)


def test_read_token_cannot_call_write_tools() -> None:
    read_only = _caller(Scope.READ.value)
    assert read_only.may_call("query.read")
    assert not read_only.may_call("query.write")
    assert not read_only.may_call("migration.apply")


def test_write_token_cannot_call_admin_tools() -> None:
    """Scope escalation must not be transitive: holding write does not imply admin,
    because restarting the container is a different kind of damage than an UPDATE."""
    writer = _caller(Scope.READ.value, Scope.WRITE.value)
    assert writer.may_call("query.write")
    assert not writer.may_call("container.restart")
    assert not writer.may_call("container.exec")


def test_unmapped_tool_requires_admin() -> None:
    """Deny by default. A tool added later without a TOOL_SCOPES entry must be locked
    down, not silently reachable by every read token."""
    assert "tool.invented.tomorrow" not in TOOL_SCOPES
    assert not _caller(Scope.READ.value).may_call("tool.invented.tomorrow")
    assert not _caller(Scope.WRITE.value).may_call("tool.invented.tomorrow")
    assert _caller(Scope.ADMIN.value).may_call("tool.invented.tomorrow")


def test_stdio_caller_is_unrestricted() -> None:
    """No token under stdio is by design (ADR-002), not a missing credential: the server
    is a subprocess the user spawned with their own DSN. Denying everything there would
    break local use for no security gain."""
    assert not LOCAL_CALLER.authenticated
    assert LOCAL_CALLER.may_call("query.write")
    assert LOCAL_CALLER.may_call("container.exec")


def test_current_caller_outside_a_request_is_local() -> None:
    assert current_caller() == LOCAL_CALLER


# --- live HTTP ------------------------------------------------------------------------


@pytest.mark.slow
async def test_scopes_are_enforced_over_http(conn_manager: object, config: object) -> None:
    """The end-to-end claim: a read-only token is genuinely incapable of writing, and a
    write token is not incapable of it."""
    from fastmcp import Client

    from pgops.__main__ import build_server

    pair = generate_keypair()
    server = build_server(config, conn_manager, auth=build_verifier(pair.public_key))  # type: ignore[arg-type]
    port = 8793
    task = asyncio.create_task(server.run_async(transport="http", host="127.0.0.1", port=port))
    await asyncio.sleep(4)
    url = f"http://127.0.0.1:{port}/mcp/"

    try:
        read_token = issue_token(pair.private_key, subject="reader")
        async with Client(url, auth=read_token) as client:
            listed = {t.name for t in await client.list_tools()}
            # Not the security boundary — the call below is — but an agent that is never
            # shown a tool does not waste a turn discovering it cannot use it.
            assert "query.read" in listed
            assert "query.write" not in listed

            with pytest.raises(Exception) as exc_info:
                await client.call_tool("query.write", {"sql": "CREATE TABLE nope (id int)"})
            assert "pgops:write" in str(exc_info.value)

        write_token = issue_token(
            pair.private_key,
            subject="writer",
            scopes=[Scope.READ.value, Scope.WRITE.value],
        )
        async with Client(url, auth=write_token) as client:
            assert "query.write" in {t.name for t in await client.list_tools()}
            result = await client.call_tool(
                "query.write", {"sql": "INSERT INTO items (name) VALUES ('scoped')"}
            )
            assert result.data["rows_affected"] == 1
    finally:
        task.cancel()
