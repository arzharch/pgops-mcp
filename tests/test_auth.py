"""Token issuance and HTTP transport auth.

The central claim under test: **stdio needs no auth, HTTP refuses to run without it.**
Auth is bound to the transport rather than being a global setting, because the reason
stdio is safe (there is no remote caller) stops being true the moment a port is open.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import free_port

from pgops.__main__ import parse_args
from pgops.auth import (
    TOOL_SCOPES,
    Scope,
    build_verifier,
    describe_scopes,
    generate_keypair,
    issue_token,
    load_public_key,
)
from pgops.errors import ErrorCode, PgopsError


@pytest.fixture(scope="module")
def keypair() -> object:
    return generate_keypair()


def test_keypair_is_asymmetric(keypair: object) -> None:
    """RS256, not a shared secret: the server holds only the public key, so a server
    compromise leaks the ability to *verify* tokens, never to *issue* them."""
    assert "PRIVATE KEY" in keypair.private_key  # type: ignore[attr-defined]
    assert "PUBLIC KEY" in keypair.public_key  # type: ignore[attr-defined]
    assert keypair.private_key != keypair.public_key  # type: ignore[attr-defined]


def test_saved_private_key_is_not_world_readable(keypair: object, tmp_path: Path) -> None:
    private_path, public_path = keypair.save(tmp_path)  # type: ignore[attr-defined]
    assert private_path.exists() and public_path.exists()
    assert "PRIVATE KEY" in private_path.read_text()


def test_token_defaults_to_read_only(keypair: object) -> None:
    """An agent that only answers questions about a schema should not hold a credential
    capable of dropping it, so read-only is what you get unless you ask otherwise."""
    token = issue_token(keypair.private_key, subject="agent-1")  # type: ignore[attr-defined]
    claims = _decode(token)
    assert claims["scope"] == Scope.READ.value
    assert claims["sub"] == "agent-1"


def test_token_can_be_granted_write(keypair: object) -> None:
    token = issue_token(
        keypair.private_key,  # type: ignore[attr-defined]
        subject="agent-2",
        scopes=[Scope.READ.value, Scope.WRITE.value],
    )
    assert "pgops:write" in _decode(token)["scope"]


def test_unknown_scope_is_refused(keypair: object) -> None:
    with pytest.raises(PgopsError) as exc_info:
        issue_token(keypair.private_key, subject="x", scopes=["pgops:everything"])  # type: ignore[attr-defined]
    assert exc_info.value.code is ErrorCode.INVALID_ARGUMENT


def test_subject_is_recorded_for_audit(keypair: object) -> None:
    """With stdio there is one caller and identity is implicit. An HTTP server has many,
    and "who ran this DELETE" has to be answerable."""
    token = issue_token(keypair.private_key, subject="ci-pipeline")  # type: ignore[attr-defined]
    assert _decode(token)["sub"] == "ci-pipeline"


def test_token_expires(keypair: object) -> None:
    token = issue_token(keypair.private_key, subject="x", expires_in_seconds=60)  # type: ignore[attr-defined]
    claims = _decode(token)
    assert claims["exp"] - claims["iat"] == 60


def test_verifier_builds_from_public_key_only(keypair: object) -> None:
    verifier = build_verifier(keypair.public_key)  # type: ignore[attr-defined]
    assert verifier is not None


def test_load_public_key_missing_file_is_structured(tmp_path: Path) -> None:
    with pytest.raises(PgopsError) as exc_info:
        load_public_key(tmp_path / "nope.pem")
    assert exc_info.value.code is ErrorCode.INVALID_ARGUMENT
    assert "keygen" in (exc_info.value.hint or "")


# --- scope mapping --------------------------------------------------------------------


def test_read_scope_covers_no_mutating_tool() -> None:
    """The scope split is only meaningful if a read token genuinely cannot mutate."""
    read_tools = {t for t, s in TOOL_SCOPES.items() if s is Scope.READ}
    assert "query.write" not in read_tools
    assert "migration.apply" not in read_tools
    assert "container.restart" not in read_tools
    assert "container.exec" not in read_tools


def test_container_mutations_require_admin() -> None:
    assert TOOL_SCOPES["container.restart"] is Scope.ADMIN
    assert TOOL_SCOPES["container.exec"] is Scope.ADMIN


def test_migration_plan_is_read_but_apply_is_write() -> None:
    """Planning executes nothing (its dry run is rolled back), so it belongs on the read
    side; applying does not."""
    assert TOOL_SCOPES["migration.plan"] is Scope.READ
    assert TOOL_SCOPES["migration.apply"] is Scope.WRITE


def test_scope_documentation_covers_every_mapped_tool() -> None:
    described = describe_scopes()
    for tool in TOOL_SCOPES:
        assert tool in described


# --- transport binding ----------------------------------------------------------------


def test_stdio_is_the_default_transport() -> None:
    assert parse_args([]).transport == "stdio"
    assert parse_args([]).public_key is None


def test_http_transport_is_opt_in() -> None:
    args = parse_args(["--transport", "http", "--public-key", "/tmp/k.pem", "--port", "9000"])
    assert args.transport == "http"
    assert args.port == 9000


def test_http_defaults_to_loopback() -> None:
    """Binding 0.0.0.0 by default would expose a database operator to the whole network
    the moment someone tried HTTP mode."""
    assert parse_args(["--transport", "http"]).host == "127.0.0.1"


def test_keygen_and_issue_token_subcommands_parse() -> None:
    assert parse_args(["keygen"]).command == "keygen"
    args = parse_args(["issue-token", "--subject", "a", "--scope", "pgops:write"])
    assert args.command == "issue-token"
    assert args.scope == ["pgops:write"]


# --- live HTTP transport --------------------------------------------------------------


@pytest.mark.slow
async def test_http_transport_rejects_unauthenticated_and_accepts_valid_tokens(
    conn_manager: object, config: object
) -> None:
    """The end-to-end claim: a database operator on a network port is unreachable
    without a valid token, and fully usable with one."""
    import asyncio

    from fastmcp import Client

    from pgops.__main__ import build_server

    pair = generate_keypair()
    server = build_server(config, conn_manager, auth=build_verifier(pair.public_key))  # type: ignore[arg-type]
    port = free_port()
    task = asyncio.create_task(server.run_async(transport="http", host="127.0.0.1", port=port))
    await asyncio.sleep(4)
    url = f"http://127.0.0.1:{port}/mcp/"

    try:
        for bad_auth in (None, "not-a-real-token"):
            with pytest.raises(Exception) as exc_info:
                async with Client(url, auth=bad_auth) as client:
                    await client.list_tools()
            assert "401" in str(exc_info.value), bad_auth

        token = issue_token(pair.private_key, subject="test-agent")
        async with Client(url, auth=token) as client:
            assert await client.list_tools()
            result = await client.call_tool("query.read", {"sql": "SELECT 1 AS n"})
            assert result.data["rows"] == [{"n": 1}]
    finally:
        task.cancel()


def _decode(token: str) -> dict[str, object]:
    import base64
    import json

    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    result: dict[str, object] = json.loads(base64.urlsafe_b64decode(payload))
    return result


def test_saved_keys_are_protected_from_being_committed(keypair: object, tmp_path: Path) -> None:
    """A tool that hands the user a credential owns the obvious way it leaks.

    `--key-dir` accepts any path, so `pgops-mcp keygen --key-dir ./keys` inside a project
    is a reasonable thing to do — and one `git add -A` later a signing key is in the
    history, where deleting it does not help: it has to be treated as compromised.
    """
    directory = tmp_path / "keys"
    private_path, _ = keypair.save(directory)  # type: ignore[attr-defined]
    guard = directory / ".gitignore"
    assert guard.exists(), "keygen must not leave a private key in an unguarded directory"
    assert guard.read_text().strip() == "*"
    assert private_path.parent == directory
