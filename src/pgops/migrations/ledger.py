"""Migration ledger: `pgops_migrations` bookkeeping, checksums, crash recovery (ADR-003).

The ledger answers three questions that a migration tool must never guess at:
  1. What has already been applied? (so re-running is safe)
  2. Has an applied migration's SQL changed since? (checksum drift — someone edited a
     migration that already ran, so the database and the definition disagree)
  3. Was something interrupted mid-flight? (crash recovery)

Question 3 is the one that shapes the schema. A naive ledger inserts a row *after* a
successful apply — which means a process killed mid-migration leaves no trace at all,
and the next run cannot tell "never started" from "half applied". So a row is written
with status `in_flight` **before** the DDL runs, and updated to `applied` or `failed`
after. A row still marked `in_flight` on startup is a crash, and the tool refuses to
proceed rather than guessing which steps landed.

Why the ledger row cannot simply be part of the migration's own transaction: it usually
is, and that is the happy path — a rolled-back migration takes its `in_flight` row with
it, which is correct and self-cleaning. But `CREATE INDEX CONCURRENTLY` cannot run
inside a transaction at all, so for those steps the ledger write and the DDL are
genuinely separate, and the `in_flight` marker is the only crash evidence that exists.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import asyncpg

LEDGER_TABLE = "pgops_migrations"

_CREATE_LEDGER_SQL = f"""
CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    migration_id text NOT NULL,
    name         text NOT NULL,
    checksum     text NOT NULL,
    status       text NOT NULL CHECK (status IN ('in_flight', 'applied', 'failed', 'rolled_back')),
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    duration_ms  double precision,
    applied_by   text,
    error        text,
    steps        jsonb NOT NULL DEFAULT '[]'::jsonb
)
"""

# One partial unique index rather than a plain one: a migration may legitimately appear
# twice if the first attempt failed and was retried, but it must never be `applied`
# twice.
_CREATE_UNIQUE_APPLIED_SQL = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {LEDGER_TABLE}_applied_uniq
ON {LEDGER_TABLE} (migration_id) WHERE status = 'applied'
"""


class MigrationStatus(StrEnum):
    IN_FLIGHT = "in_flight"
    APPLIED = "applied"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


def checksum_steps(steps: list[str]) -> str:
    """Hash of the exact SQL, in order.

    Order is part of the identity: the same statements applied in a different sequence
    can produce a different schema (or fail outright), so a set-based hash would call
    two genuinely different migrations identical.
    """
    digest = hashlib.sha256()
    for step in steps:
        digest.update(step.encode("utf-8"))
        digest.update(b"\x00")  # separator: prevents ["ab","c"] hashing as ["a","bc"]
    return digest.hexdigest()


@dataclass(slots=True)
class LedgerEntry:
    migration_id: str
    name: str
    checksum: str
    status: MigrationStatus
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: float | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "migration_id": self.migration_id,
            "name": self.name,
            "checksum": self.checksum,
            "status": self.status.value,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_ms": self.duration_ms,
            "error": self.error,
        }


class MigrationLedger:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def ensure_table(self) -> None:
        await self._conn.execute(_CREATE_LEDGER_SQL)
        await self._conn.execute(_CREATE_UNIQUE_APPLIED_SQL)

    async def find_in_flight(self) -> list[LedgerEntry]:
        rows = await self._conn.fetch(
            f"SELECT * FROM {LEDGER_TABLE} WHERE status = 'in_flight' ORDER BY started_at"
        )
        return [_row_to_entry(r) for r in rows]

    async def get_applied(self, migration_id: str) -> LedgerEntry | None:
        row = await self._conn.fetchrow(
            f"SELECT * FROM {LEDGER_TABLE} WHERE migration_id = $1 AND status = 'applied'",
            migration_id,
        )
        return _row_to_entry(row) if row else None

    async def history(self, limit: int = 50) -> list[LedgerEntry]:
        rows = await self._conn.fetch(
            f"SELECT * FROM {LEDGER_TABLE} ORDER BY started_at DESC LIMIT $1", limit
        )
        return [_row_to_entry(r) for r in rows]

    async def begin(
        self, migration_id: str, name: str, checksum: str, steps: list[str], applied_by: str
    ) -> int:
        """Record intent BEFORE running any DDL — see module docstring."""
        import json

        row_id: int = await self._conn.fetchval(
            f"""
            INSERT INTO {LEDGER_TABLE}
                (migration_id, name, checksum, status, applied_by, steps)
            VALUES ($1, $2, $3, 'in_flight', $4, $5::jsonb)
            RETURNING id
            """,
            migration_id,
            name,
            checksum,
            applied_by,
            json.dumps(steps),
        )
        return row_id

    async def finish(self, row_id: int, duration_ms: float) -> None:
        await self._conn.execute(
            f"""
            UPDATE {LEDGER_TABLE}
            SET status = 'applied', finished_at = now(), duration_ms = $2
            WHERE id = $1
            """,
            row_id,
            duration_ms,
        )

    async def fail(self, row_id: int, error: str) -> None:
        await self._conn.execute(
            f"""
            UPDATE {LEDGER_TABLE}
            SET status = 'failed', finished_at = now(), error = $2
            WHERE id = $1
            """,
            row_id,
            error[:2000],
        )

    async def mark_rolled_back(self, migration_id: str) -> None:
        await self._conn.execute(
            f"""
            UPDATE {LEDGER_TABLE}
            SET status = 'rolled_back', finished_at = now()
            WHERE migration_id = $1 AND status = 'applied'
            """,
            migration_id,
        )


def _row_to_entry(row: asyncpg.Record) -> LedgerEntry:
    return LedgerEntry(
        migration_id=row["migration_id"],
        name=row["name"],
        checksum=row["checksum"],
        status=MigrationStatus(row["status"]),
        started_at=row["started_at"] or datetime.now(UTC),
        finished_at=row["finished_at"],
        duration_ms=row["duration_ms"],
        error=row["error"],
    )
