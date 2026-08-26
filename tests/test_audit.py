"""Audit log: format, durability, and — most importantly — that refusals are recorded."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pgops.audit import AuditEntry, AuditLog, sql_fingerprint
from pgops.config import PgopsConfig
from pgops.connections import ConnectionManager
from pgops.errors import PgopsError
from pgops.guardrails import ConfirmationTokenStore
from pgops.tools.write import query_write


def test_entry_has_hash_and_timestamp(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.record(
        AuditEntry(
            tool="query.write", sql="DELETE FROM t", verdict="executed", classification="write"
        )
    )
    (entry,) = log.read_all()
    assert entry["sql"] == "DELETE FROM t"
    assert entry["sql_sha256"] == sql_fingerprint("DELETE FROM t")
    assert entry["ts"].endswith("+00:00")  # timezone-aware UTC, not naive local time
    assert entry["audit_id"]


def test_log_is_append_only(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    for i in range(5):
        log.record(
            AuditEntry(
                tool="query.write", sql=f"SELECT {i}", verdict="executed", classification="write"
            )
        )
    entries = log.read_all()
    assert [e["sql"] for e in entries] == [f"SELECT {i}" for i in range(5)]


def test_creates_parent_directory(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "nested" / "deeper" / "audit.jsonl")
    log.record(AuditEntry(tool="t", sql="SELECT 1", verdict="executed", classification="read"))
    assert log.path.exists()


def test_one_json_object_per_line(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.record(
        AuditEntry(tool="t", sql="SELECT 'multi\nline'", verdict="executed", classification="read")
    )
    log.record(AuditEntry(tool="t", sql="SELECT 2", verdict="executed", classification="read"))
    # embedded newlines must not break the line-per-entry contract
    lines = log.path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    for line in lines:
        json.loads(line)


def test_tolerates_torn_final_line(tmp_path: Path) -> None:
    """A process killed mid-write leaves a partial line; everything before it must
    still be readable."""
    log = AuditLog(tmp_path / "audit.jsonl")
    log.record(AuditEntry(tool="t", sql="SELECT 1", verdict="executed", classification="read"))
    with log.path.open("a", encoding="utf-8") as fh:
        fh.write('{"ts": "truncated mid-w')
    entries = log.read_all()
    assert len(entries) == 1


def test_write_failure_does_not_raise(tmp_path: Path) -> None:
    """An unwritable audit path must not take down a tool call that otherwise
    succeeded — it logs loudly instead."""
    log = AuditLog(tmp_path / "audit.jsonl" / "impossible.jsonl")
    log.record(AuditEntry(tool="t", sql="SELECT 1", verdict="executed", classification="read"))


async def test_refusal_is_audited(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    tokens: ConfirmationTokenStore,
) -> None:
    """The event an incident review most needs is the one that was blocked. A design
    that only logs executed statements would show nothing here."""
    log = AuditLog(config.audit_path)
    with pytest.raises(PgopsError):
        await query_write(conn_manager, config, log, tokens, "DELETE FROM items")

    (entry,) = log.read_all()
    assert entry["verdict"] == "refused_pending_confirmation"
    assert entry["sql"] == "DELETE FROM items"
    assert "every row" in entry["detail"]


async def test_execution_is_audited_with_rows_and_duration(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    tokens: ConfirmationTokenStore,
) -> None:
    log = AuditLog(config.audit_path)
    result = await query_write(conn_manager, config, log, tokens, "DELETE FROM items WHERE id <= 5")
    (entry,) = log.read_all()
    assert entry["verdict"] == "executed"
    assert entry["rows_affected"] == 5
    assert entry["duration_ms"] > 0
    assert entry["audit_id"] == result.audit_id


async def test_full_confirm_flow_leaves_complete_trail(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    tokens: ConfirmationTokenStore,
) -> None:
    """An incident reviewer should be able to reconstruct: it was attempted, it was
    blocked, someone approved it, it ran, and this many rows died."""
    log = AuditLog(config.audit_path)
    sql = "DELETE FROM items"
    with pytest.raises(PgopsError) as exc_info:
        await query_write(conn_manager, config, log, tokens, sql)
    token = (exc_info.value.hint or "").split("confirm_token=")[1].split("'")[1]
    await query_write(conn_manager, config, log, tokens, sql, confirm_token=token)

    refusal, execution = log.read_all()
    assert refusal["verdict"] == "refused_pending_confirmation"
    assert execution["verdict"] == "executed"
    assert execution["rows_affected"] == 250
    # same statement across both records, linkable by hash without string matching
    assert refusal["sql_sha256"] == execution["sql_sha256"]


async def test_bad_token_attempt_is_audited(
    conn_manager: ConnectionManager,
    config: PgopsConfig,
    tokens: ConfirmationTokenStore,
) -> None:
    log = AuditLog(config.audit_path)
    token = tokens.issue("DELETE FROM items WHERE id = 1", "test")
    with pytest.raises(PgopsError):
        await query_write(
            conn_manager, config, log, tokens, "DELETE FROM items", confirm_token=token
        )
    (entry,) = log.read_all()
    assert entry["verdict"] == "refused_bad_token"
    assert entry["error_code"] == "CONFIRMATION_MISMATCH"
