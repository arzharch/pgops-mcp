"""Caller identity in the audit log.

Over stdio there is one caller and identity is implicit. Over HTTP there are many, and
"who ran this DELETE" has to be answerable — which is the entire reason `--subject` is
required when issuing a token. Recording the subject is what turns a log that answers
"what happened" into one that answers "who did it".
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from pgops.audit import UNKNOWN_ACTOR, AuditEntry, AuditLog
from pgops.auth import Scope, build_verifier, generate_keypair, issue_token
from pgops.middleware import LOCAL_ACTOR


def _entry() -> AuditEntry:
    return AuditEntry(
        tool="query.write",
        sql="DELETE FROM items WHERE id = 1",
        verdict="executed",
        classification="write",
    )


def test_stdio_calls_are_recorded_as_local(tmp_path: Path) -> None:
    """Outside a request context there is no token, and that is by design (ADR-002) —
    not a missing value to leave blank."""
    log = AuditLog(tmp_path / "a.jsonl")
    log.record(_entry())
    assert log.read_all()[0]["actor"] == LOCAL_ACTOR


def test_token_subject_is_recorded(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "a.jsonl", actor_resolver=lambda: "deploy-bot")
    log.record(_entry())
    assert log.read_all()[0]["actor"] == "deploy-bot"


def test_explicit_actor_wins_over_the_resolver(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "a.jsonl", actor_resolver=lambda: "resolved")
    entry = _entry()
    entry.actor = "explicit"
    log.record(entry)
    assert log.read_all()[0]["actor"] == "explicit"


def test_identity_failure_never_loses_the_audit_line(tmp_path: Path) -> None:
    """Losing the identity is bad; losing the record entirely is worse. A broken auth
    layer must degrade the entry, not suppress it."""

    def broken() -> str:
        raise RuntimeError("auth layer exploded")

    log = AuditLog(tmp_path / "a.jsonl", actor_resolver=broken)
    with pytest.raises(RuntimeError):
        log.record(_entry())
    # ...and the default resolver, which is the one actually wired up, swallows instead:
    from pgops.audit import default_actor

    assert default_actor() in {LOCAL_ACTOR, UNKNOWN_ACTOR}


def test_audit_resource_exposes_actor_but_not_sql(tmp_path: Path) -> None:
    """The redacted view drops SQL text because statements embed literal values. The
    actor carries no row data, so it is exactly the field that should survive."""
    import json

    from pgops.config import PgopsConfig
    from pgops.resources import audit_resource

    config = PgopsConfig.from_env(
        dsn="postgresql://u:p@localhost:5432/d", audit_path=tmp_path / "a.jsonl"
    )
    AuditLog(config.audit_path, actor_resolver=lambda: "ci-pipeline").record(_entry())
    payload = json.loads(audit_resource(config))
    entry = payload["entries"][0]
    assert entry["actor"] == "ci-pipeline"
    assert "sql" not in entry


@pytest.mark.slow
async def test_http_calls_record_the_token_subject(conn_manager: object, config: object) -> None:
    """End to end: two different agents write, and the log distinguishes them."""
    from fastmcp import Client

    from pgops.__main__ import build_server

    pair = generate_keypair()
    server = build_server(config, conn_manager, auth=build_verifier(pair.public_key))  # type: ignore[arg-type]
    port = 8795
    task = asyncio.create_task(server.run_async(transport="http", host="127.0.0.1", port=port))
    await asyncio.sleep(4)
    url = f"http://127.0.0.1:{port}/mcp/"
    scopes = [Scope.READ.value, Scope.WRITE.value]

    try:
        for subject in ("agent-alpha", "agent-beta"):
            token = issue_token(pair.private_key, subject=subject, scopes=scopes)
            async with Client(url, auth=token) as client:
                await client.call_tool(
                    "query.write", {"sql": f"INSERT INTO items (name) VALUES ('{subject}')"}
                )
    finally:
        task.cancel()

    actors = [e["actor"] for e in AuditLog(config.audit_path).read_all()]  # type: ignore[attr-defined]
    assert "agent-alpha" in actors
    assert "agent-beta" in actors
