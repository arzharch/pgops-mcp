"""Audit replay â€” dry-run analysis and gated execution.

The audit log is the forensic record; replay makes it executable. Tests cover:

- only executed statements are considered (refusals are history, not instructions)
- dry-run classifies under *current* rules and flags divergence
- execute mode actually runs statements and records replay entries with a
  `replay:` actor prefix so replayed traffic is distinguishable in later reviews
- a failing statement stops the run at that point (partial replay beats silent
  divergence â€” skipped writes produce a state matching no point in the timeline)
- chronological order is preserved regardless of file append order

ADR-005 applies where a database is involved.
"""

from __future__ import annotations

import json
from pathlib import Path

import asyncpg
import pytest

from pgops.audit import AuditLog
from pgops.config import PgopsConfig
from pgops.connections import ConnectionManager
from pgops.replay import replay_audit_log


def _write_audit(path: Path, entries: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


@pytest.fixture
def audit_file(tmp_path: Path) -> Path:
    return tmp_path / "audit.jsonl"


async def test_dry_run_flags_nothing_for_clean_history(audit_file: Path) -> None:
    _write_audit(
        audit_file,
        [
            {
                "ts": "t1",
                "actor": "dev",
                "tool": "query.write",
                "verdict": "executed",
                "classification": "write",
                "sql": "INSERT INTO items (name) VALUES ('a')",
            }
        ],
    )
    report = await replay_audit_log(None, audit_file, execute=False)  # type: ignore[arg-type]
    assert report.executed_entries == 1
    assert report.replayed == 1
    assert report.divergent == 0
    assert report.failed_at is None


async def test_dry_run_detects_classification_divergence(audit_file: Path) -> None:
    """An entry claiming a write was classified 'read' would mean either the old
    classifier was wrong or the statement is ambiguous â€” both must be flagged."""
    _write_audit(
        audit_file,
        [
            {
                "ts": "t1",
                "actor": "dev",
                "tool": "query.write",
                "verdict": "executed",
                "classification": "read",  # lie / historical bug
                "sql": "DELETE FROM items WHERE id = 1",
            }
        ],
    )
    report = await replay_audit_log(None, audit_file, execute=False)  # type: ignore[arg-type]
    assert report.divergent == 1
    assert report.rows[0].status == "divergent"


async def test_refusals_are_skipped_not_replayed(audit_file: Path) -> None:
    """Re-running refusals would defeat the guardrail that refused them."""
    _write_audit(
        audit_file,
        [
            {
                "ts": "t1",
                "actor": "agent",
                "tool": "query.write",
                "verdict": "refused_pending_confirmation",
                "classification": "write",
                "sql": "DELETE FROM items",
            },
            {
                "ts": "t2",
                "actor": "agent",
                "tool": "query.write",
                "verdict": "executed",
                "classification": "write",
                "sql": "SELECT 1",
            },
        ],
    )
    report = await replay_audit_log(None, audit_file, execute=False)  # type: ignore[arg-type]
    assert report.skipped == 1
    assert report.executed_entries == 1


async def test_actor_filter_limits_scope(audit_file: Path) -> None:
    _write_audit(
        audit_file,
        [
            {
                "ts": "t1",
                "actor": "alice",
                "tool": "query.read",
                "verdict": "executed",
                "classification": "read",
                "sql": "SELECT 1",
            },
            {
                "ts": "t2",
                "actor": "bob",
                "tool": "query.read",
                "verdict": "executed",
                "classification": "read",
                "sql": "SELECT 2",
            },
        ],
    )
    report = await replay_audit_log(None, audit_file, execute=False, actor_filter="alice")  # type: ignore[arg-type]
    assert report.total_entries == 1
    assert report.rows[0].actor == "alice"


# --- live execution against real Postgres -----------------------------------------------


async def test_execute_mode_runs_statements_and_audits_as_replay(
    conn_manager: ConnectionManager, config: PgopsConfig
) -> None:
    log_path = config.audit_path
    _write_audit(
        log_path,
        [
            {
                "ts": "t1",
                "actor": "original-agent",
                "tool": "query.write",
                "verdict": "executed",
                "classification": "write",
                "sql": "CREATE TABLE replay_test (id int)",
            },
            {
                "ts": "t2",
                "actor": "original-agent",
                "tool": "query.write",
                "verdict": "executed",
                "classification": "write",
                "sql": "INSERT INTO replay_test VALUES (42)",
            },
        ],
    )
    try:
        report = await replay_audit_log(conn_manager, log_path, execute=True)
        assert report.failed_at is None
        assert report.replayed == 2

        # rows actually landed
        conn = await asyncpg.connect(config.dsn)
        try:
            n = await conn.fetchval("SELECT count(*) FROM replay_test")
            assert n == 1
        finally:
            await conn.close()

        # replay itself was audited with a distinguishable actor
        entries = AuditLog(log_path).read_all()
        replay_entries = [e for e in entries if e.get("tool") == "replay"]
        assert len(replay_entries) == 2
        assert all(e["actor"].startswith("replay:") for e in replay_entries)
        assert any("original-agent" in e.get("detail", "") for e in replay_entries)
    finally:
        cleanup = await asyncpg.connect(config.dsn)
        try:
            await cleanup.execute("DROP TABLE IF EXISTS replay_test")
        finally:
            await cleanup.close()


async def test_failed_statement_stops_the_run(
    conn_manager: ConnectionManager, config: PgopsConfig
) -> None:
    """A statement referencing a missing table fails; everything after it must NOT
    run â€” partial replay is safer than skipping errors silently."""
    log_path = config.audit_path
    _write_audit(
        log_path,
        [
            {
                "ts": "t1",
                "actor": "x",
                "tool": "query.write",
                "verdict": "executed",
                "classification": "write",
                "sql": "CREATE TABLE replay_ok (id int)",
            },
            {
                "ts": "t2",
                "actor": "x",
                "tool": "query.write",
                "verdict": "executed",
                "classification": "write",
                "sql": "INSERT INTO no_such_table_anywhere VALUES (1)",
            },
            {
                "ts": "t3",
                "actor": "x",
                "tool": "query.write",
                "verdict": "executed",
                "classification": "write",
                "sql": "CREATE TABLE replay_never (id int)",
            },
        ],
    )
    try:
        report = await replay_audit_log(conn_manager, log_path, execute=True)
        assert report.failed_at is not None
        statuses = [r.status for r in report.rows if r.status != "skipped"]
        # first succeeded, second failed, third never attempted
        assert statuses == ["replayed", "failed"]

        conn = await asyncpg.connect(config.dsn)
        try:
            exists = await conn.fetchval("SELECT to_regclass('replay_never') IS NOT NULL")
            assert exists is False, "statements after the failure must not have run"
        finally:
            await conn.close()
    finally:
        cleanup = await asyncpg.connect(config.dsn)
        try:
            await cleanup.execute("DROP TABLE IF EXISTS replay_ok")
        finally:
            await cleanup.close()


async def test_chronological_order_regardless_of_file_order(audit_file: Path) -> None:
    """Append-only JSONL is chronological by construction; replay must preserve that
    order (oldest first) so dependent statements run in their original sequence."""
    _write_audit(
        audit_file,
        [
            {
                "ts": "early",
                "actor": "a",
                "tool": "query.read",
                "verdict": "executed",
                "classification": "read",
                "sql": "SELECT 1",
            },
            {
                "ts": "late",
                "actor": "a",
                "tool": "query.read",
                "verdict": "executed",
                "classification": "read",
                "sql": "SELECT 2",
            },
        ],
    )
    report = await replay_audit_log(None, audit_file, execute=False)  # type: ignore[arg-type]
    assert [r.ts for r in report.rows] == ["early", "late"]


async def test_replay_entries_from_prior_runs_are_skipped(audit_file: Path) -> None:
    """Replaying a live audit file must not compound its own replay output: entries
    with tool == "replay" are history of a previous replay, not instructions."""
    _write_audit(
        audit_file,
        [
            {
                "ts": "t1",
                "actor": "replay:dev",
                "tool": "replay",
                "verdict": "executed",
                "classification": "write",
                "sql": "INSERT INTO items VALUES (999)",
            },
            {
                "ts": "t2",
                "actor": "dev",
                "tool": "query.write",
                "verdict": "executed",
                "classification": "read",
                "sql": "SELECT 1",
            },
        ],
    )
    report = await replay_audit_log(None, audit_file, execute=False)  # type: ignore[arg-type]
    assert report.executed_entries == 1
    assert report.skipped == 1
    assert report.rows[0].status == "skipped"
    assert report.rows[0].detail == "prior replay output"
