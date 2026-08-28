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
import json
from dataclasses import dataclass, field
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
    id: int
    migration_id: str
    name: str
    checksum: str
    status: MigrationStatus
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: float | None = None
    error: str | None = None
    # The recorded forward steps. Structured dicts (kind/table/target/sql) for
    # migrations applied by the current engine version; plain SQL strings for anything
    # applied before structured recording existed. Rollback refuses the string form —
    # inverting SQL text by parsing it is exactly the guessing this project refuses to do.
    steps: list[Any] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            # migration.rollback takes `ledger_id`, an integer, and its description
            # tells the caller to get it "from migration.history" — which returns these
            # dicts. Omitting `id` here meant history emitted only the text
            # `migration_id`, so the integer rollback needs appeared nowhere in the MCP
            # surface and the documented flow could not be completed by any client.
            "ledger_id": self.id,
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

    async def get_by_id(self, row_id: int) -> LedgerEntry | None:
        """Fetch one ledger row by its primary key — how rollback addresses a migration."""
        row = await self._conn.fetchrow(f"SELECT * FROM {LEDGER_TABLE} WHERE id = $1", row_id)
        return _row_to_entry(row) if row else None

    async def applied_after(self, started_at: datetime) -> list[LedgerEntry]:
        """Applied migrations recorded after `started_at` — the stack check.

        Reversing an earlier change while later migrations sit on top of it produces
        failures that are hard to unwind, so rollback refuses rather than picking that
        fight silently.
        """
        rows = await self._conn.fetch(
            f"""
            SELECT * FROM {LEDGER_TABLE}
            WHERE status = 'applied' AND started_at > $1
            ORDER BY started_at
            """,
            started_at,
        )
        return [_row_to_entry(r) for r in rows]

    async def history(self, limit: int = 50) -> list[LedgerEntry]:
        rows = await self._conn.fetch(
            f"SELECT * FROM {LEDGER_TABLE} ORDER BY started_at DESC LIMIT $1", limit
        )
        return [_row_to_entry(r) for r in rows]

    async def begin(
        self,
        migration_id: str,
        name: str,
        checksum: str,
        steps: list[str],
        applied_by: str,
        step_details: list[dict[str, Any]] | None = None,
    ) -> int:
        """Record intent BEFORE running any DDL — see module docstring.

        `step_details` records the *structured* form of each step (kind/table/target)
        alongside the SQL. Rollback needs the structure: inverting SQL text by parsing
        it is exactly the guessing this project refuses to do.
        """
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
            json.dumps(step_details if step_details is not None else steps),
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

    async def resolve_in_flight(self, row_id: int, status: str, note: str) -> LedgerEntry | None:
        """Close out an interrupted migration once a human has decided what happened.

        A crash between `begin` and `finish` leaves a row `in_flight`, and every
        subsequent apply refuses while one exists — correctly, because pgops cannot know
        whether the DDL committed. Until this existed the only way out was to UPDATE the
        ledger by hand in psql, which brought the whole migration subsystem down to
        "connect to the database yourself" after any crash. That is the situation an MCP
        server is supposed to prevent, not create.

        This does not inspect the schema and does not guess: the caller states the
        outcome and it is recorded verbatim, with the note, under their identity.
        """
        row = await self._conn.fetchrow(
            f"""
            UPDATE {LEDGER_TABLE}
            SET status = $2, finished_at = now(),
                -- concat_ws is variadic "any", so the parameter needs an explicit type
                -- or Postgres cannot infer one and rejects the prepare.
                error = concat_ws(' | ', error, $3::text)
            WHERE id = $1 AND status = 'in_flight'
            RETURNING *
            """,
            row_id,
            status,
            note,
        )
        return _row_to_entry(row) if row else None

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
    steps_raw = row["steps"]
    # jsonb arrives as a list already; tolerate a JSON string for robustness.
    if isinstance(steps_raw, str):
        try:
            steps: list[Any] = json.loads(steps_raw)
        except json.JSONDecodeError:
            steps = []
    else:
        steps = list(steps_raw or [])
    return LedgerEntry(
        id=row["id"],
        migration_id=row["migration_id"],
        name=row["name"],
        checksum=row["checksum"],
        status=MigrationStatus(row["status"]),
        started_at=row["started_at"] or datetime.now(UTC),
        finished_at=row["finished_at"],
        duration_ms=row["duration_ms"],
        error=row["error"],
        steps=steps,
    )
