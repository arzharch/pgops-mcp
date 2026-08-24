"""Lock-impact analysis for DDL steps (ADR-004).

The claim this module makes is narrow and deliberate: **operation class × table size →
an estimate with a stated confidence and plain-language reasoning.** Not a guarantee.
Exact lock duration depends on storage speed, cache state, concurrent load, and
checkpoint timing — none of which are observable from outside. Presenting a fabricated
"this will take 4.2 seconds" would be worse than useless, because someone would plan a
maintenance window around it.

The insight the whole module is built on, verified against Postgres 16 rather than
recalled:

    ALTER TABLE t ADD COLUMN b int NOT NULL DEFAULT 7;   -- relfilenode UNCHANGED
    ALTER TABLE t ALTER COLUMN a TYPE bigint;            -- relfilenode CHANGED
    ALTER TABLE t ADD COLUMN c int DEFAULT random();     -- relfilenode CHANGED

All three take an `AccessExclusiveLock` — the strictest lock Postgres has, blocking even
`SELECT`. So **lock mode alone tells you almost nothing about danger.** What separates a
non-event from a six-minute outage is whether the operation rewrites or scans the table
while holding that lock. A metadata-only change holds AccessExclusive for microseconds;
a rewrite holds it for as long as it takes to copy every row.

That is also why the constant-vs-volatile DEFAULT distinction matters so much: they look
nearly identical in SQL and differ by a full table rewrite. Postgres 11 added the
optimization for non-volatile defaults only.

Estimate rates are deliberately conservative. Measured on the development machine:
a rewrite ran at ~500k rows/s and an index build at ~650k rows/s. The constants below
are roughly half that, because for a safety tool the dangerous direction to be wrong in
is *optimistic* — a user told "2 seconds" who then experiences two minutes of downtime
on slower production storage was actively misled by this tool.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

# Conservative: roughly half of measured local throughput (see module docstring).
REWRITE_ROWS_PER_SEC = 250_000
INDEX_BUILD_ROWS_PER_SEC = 300_000
SCAN_ROWS_PER_SEC = 1_000_000

# Below this a table is small enough that even a rewrite is effectively instant, and
# saying "high risk" about it would be crying wolf.
SMALL_TABLE_ROWS = 50_000


class OperationClass(StrEnum):
    METADATA_ONLY = "metadata_only"
    TABLE_REWRITE = "table_rewrite"
    TABLE_SCAN = "table_scan"
    INDEX_BUILD = "index_build"
    INDEX_BUILD_CONCURRENT = "index_build_concurrent"
    DROP_OBJECT = "drop_object"
    UNKNOWN = "unknown"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(slots=True)
class LockImpact:
    operation: OperationClass
    lock_mode: str
    blocks_reads: bool
    blocks_writes: bool
    rewrites_table: bool
    estimate_ms: int | None
    confidence: Confidence
    reasoning: str
    safe_alternative: str | None = None
    transactional: bool = True

    @property
    def risk(self) -> str:
        """Risk is duration × *what is blocked*, not duration alone.

        Four seconds of AccessExclusiveLock — which blocks even SELECT — is a
        user-visible outage on a production table. Four seconds blocking only writes is
        a slow deploy. Ranking both the same would either cry wolf about index builds
        or wave through a genuine outage; the threshold is therefore five times
        stricter when reads are blocked.
        """
        if self.operation is OperationClass.UNKNOWN:
            return "unknown"
        if not self.blocks_reads and not self.blocks_writes:
            return "low"
        if self.estimate_ms is None:
            return "medium"
        high_threshold = 1_000 if self.blocks_reads else 5_000
        if self.estimate_ms < 100:
            return "low"
        if self.estimate_ms < high_threshold:
            return "medium"
        return "high"

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation.value,
            "risk": self.risk,
            "lock_mode": self.lock_mode,
            "blocks_reads": self.blocks_reads,
            "blocks_writes": self.blocks_writes,
            "rewrites_table": self.rewrites_table,
            "estimate_ms": self.estimate_ms,
            "confidence": self.confidence.value,
            "reasoning": self.reasoning,
            "safe_alternative": self.safe_alternative,
            "transactional": self.transactional,
        }


# Functions Postgres treats as non-volatile enough to skip the rewrite. A DEFAULT that
# is a literal, or one of these, is stored as a table-level default and existing rows
# are filled in on read — no rewrite. Anything else (random(), now() as an expression,
# a user function of unknown volatility) forces one.
_CONSTANT_DEFAULT_RE = re.compile(
    r"""^\s*default\s+(
        -?\d+(\.\d+)?            # numeric literal
      | '[^']*'(::[\w\s\."]+)?   # quoted literal, optionally cast
      | true|false|null
      | current_timestamp|now\(\)   # stable within a transaction; PG stores as constant
    )\s*$""",
    re.IGNORECASE | re.VERBOSE,
)


def _estimate_ms(rows: int, rate_per_sec: int) -> int:
    return max(int(rows / rate_per_sec * 1000), 1)


def analyze_statement(sql: str, table_rows: int = 0) -> LockImpact:
    """Classify one DDL statement and estimate its impact on a table of `table_rows`."""
    normalized = " ".join(sql.strip().split())
    upper = normalized.upper()

    if upper.startswith(("CREATE INDEX CONCURRENTLY", "CREATE UNIQUE INDEX CONCURRENTLY")):
        return LockImpact(
            operation=OperationClass.INDEX_BUILD_CONCURRENT,
            lock_mode="ShareUpdateExclusiveLock",
            blocks_reads=False,
            blocks_writes=False,
            rewrites_table=False,
            # Two passes over the table plus a wait for concurrent transactions, so
            # slower in wall-clock than a plain build — but it blocks nothing.
            estimate_ms=_estimate_ms(table_rows, INDEX_BUILD_ROWS_PER_SEC) * 2,
            confidence=Confidence.MEDIUM,
            reasoning=(
                "CONCURRENTLY builds the index without blocking reads or writes. It takes "
                "longer than a plain build and cannot run inside a transaction; if it "
                "fails it leaves an INVALID index that must be dropped and rebuilt."
            ),
            transactional=False,
        )

    if upper.startswith(("CREATE INDEX", "CREATE UNIQUE INDEX")):
        return LockImpact(
            operation=OperationClass.INDEX_BUILD,
            lock_mode="ShareLock",
            blocks_reads=False,
            blocks_writes=True,
            rewrites_table=False,
            estimate_ms=_estimate_ms(table_rows, INDEX_BUILD_ROWS_PER_SEC),
            confidence=Confidence.MEDIUM,
            reasoning=(
                "A plain CREATE INDEX takes a ShareLock: reads continue, but every "
                "INSERT/UPDATE/DELETE on the table blocks until the build finishes."
            ),
            safe_alternative=(
                normalized.replace("CREATE INDEX", "CREATE INDEX CONCURRENTLY", 1)
                if "CONCURRENTLY" not in upper and table_rows > SMALL_TABLE_ROWS
                else None
            ),
        )

    if upper.startswith("ALTER TABLE"):
        return _analyze_alter_table(normalized, upper, table_rows)

    if upper.startswith(("DROP TABLE", "DROP INDEX")):
        concurrent = "CONCURRENTLY" in upper
        return LockImpact(
            operation=OperationClass.DROP_OBJECT,
            lock_mode="ShareUpdateExclusiveLock" if concurrent else "AccessExclusiveLock",
            blocks_reads=not concurrent,
            blocks_writes=not concurrent,
            rewrites_table=False,
            estimate_ms=1,
            confidence=Confidence.HIGH,
            reasoning=(
                "Dropping an object is fast, but it must wait for every existing "
                "transaction touching the table to finish — on a busy table the *wait* "
                "is the outage, not the drop. Set lock_timeout so it fails fast instead "
                "of queueing behind a long query and blocking everything behind it."
            ),
            transactional=not concurrent,
        )

    if upper.startswith("CREATE TABLE"):
        return LockImpact(
            operation=OperationClass.METADATA_ONLY,
            lock_mode="AccessExclusiveLock",
            blocks_reads=False,
            blocks_writes=False,
            rewrites_table=False,
            estimate_ms=1,
            confidence=Confidence.HIGH,
            reasoning="Creating a new table locks only the table being created, which "
            "nothing else can be using yet.",
        )

    return LockImpact(
        operation=OperationClass.UNKNOWN,
        lock_mode="unknown",
        blocks_reads=True,
        blocks_writes=True,
        rewrites_table=False,
        estimate_ms=None,
        confidence=Confidence.LOW,
        reasoning=(
            "This statement is not in the known-pattern library, so no honest estimate "
            "can be given. Assume it takes a strict lock until proven otherwise."
        ),
    )


def _analyze_alter_table(normalized: str, upper: str, rows: int) -> LockImpact:
    # Constraint clauses are matched BEFORE the ADD COLUMN branch. `ADD CONSTRAINT c
    # CHECK (...)` otherwise matches the permissive "ADD <name> <type>" pattern below
    # (which exists because the COLUMN keyword is optional) and gets misreported as a
    # harmless metadata-only column add — hiding a full-table validation scan.
    if " VALIDATE CONSTRAINT " in upper:
        return LockImpact(
            operation=OperationClass.TABLE_SCAN,
            lock_mode="ShareUpdateExclusiveLock",
            blocks_reads=False,
            blocks_writes=False,
            rewrites_table=False,
            estimate_ms=_estimate_ms(rows, SCAN_ROWS_PER_SEC),
            confidence=Confidence.MEDIUM,
            reasoning=(
                "VALIDATE CONSTRAINT scans the table but takes only a "
                "ShareUpdateExclusiveLock — reads and writes both continue. This is the "
                "second half of the safe two-step constraint pattern."
            ),
        )

    if " ADD CONSTRAINT " in upper:
        if "NOT VALID" in upper:
            return LockImpact(
                operation=OperationClass.METADATA_ONLY,
                lock_mode="ShareRowExclusiveLock",
                blocks_reads=False,
                blocks_writes=True,
                rewrites_table=False,
                estimate_ms=1,
                confidence=Confidence.HIGH,
                reasoning=(
                    "ADD CONSTRAINT ... NOT VALID records the constraint without checking "
                    "existing rows, so it returns immediately. New rows are enforced from "
                    "now on; run VALIDATE CONSTRAINT separately to check existing ones."
                ),
            )
        is_fk = " FOREIGN KEY " in upper
        return LockImpact(
            operation=OperationClass.TABLE_SCAN,
            lock_mode="ShareRowExclusiveLock" if is_fk else "AccessExclusiveLock",
            blocks_reads=False,
            blocks_writes=True,
            rewrites_table=False,
            estimate_ms=_estimate_ms(rows, SCAN_ROWS_PER_SEC),
            confidence=Confidence.MEDIUM,
            reasoning=(
                "Adding a validated constraint scans every existing row to check it, "
                "blocking writes for the duration."
            ),
            safe_alternative=(
                "Add it as NOT VALID first (instant, enforces new rows), then run "
                "VALIDATE CONSTRAINT, which scans without blocking reads or writes."
            ),
        )

    # --- ADD COLUMN: the constant-vs-volatile DEFAULT split (see module docstring) ---
    if " ADD COLUMN " in upper or re.search(r"\bADD\s+(COLUMN\s+)?\w+\s+\w", upper):
        if " DEFAULT " in upper:
            default_clause = "DEFAULT " + normalized.upper().split(" DEFAULT ", 1)[1]
            is_constant = bool(_CONSTANT_DEFAULT_RE.match(default_clause))
            if not is_constant:
                return LockImpact(
                    operation=OperationClass.TABLE_REWRITE,
                    lock_mode="AccessExclusiveLock",
                    blocks_reads=True,
                    blocks_writes=True,
                    rewrites_table=True,
                    estimate_ms=_estimate_ms(rows, REWRITE_ROWS_PER_SEC),
                    confidence=Confidence.MEDIUM,
                    reasoning=(
                        "ADD COLUMN with a VOLATILE default rewrites the entire table, "
                        "holding AccessExclusiveLock (blocking reads AND writes) for the "
                        "whole rewrite. Postgres 11+ skips the rewrite only for "
                        "non-volatile defaults."
                    ),
                    safe_alternative=(
                        "Split it: ADD COLUMN with no default (instant), then backfill in "
                        "batches, then SET DEFAULT for future rows."
                    ),
                )
        return LockImpact(
            operation=OperationClass.METADATA_ONLY,
            lock_mode="AccessExclusiveLock",
            blocks_reads=True,
            blocks_writes=True,
            rewrites_table=False,
            estimate_ms=1,
            confidence=Confidence.HIGH,
            reasoning=(
                "ADD COLUMN with no default, or with a non-volatile one, is a catalog "
                "change only — Postgres 11+ does not rewrite the table. It still takes "
                "AccessExclusiveLock, but holds it for microseconds. The risk is not the "
                "change, it is waiting to acquire the lock behind a long-running query."
            ),
        )

    if " ALTER COLUMN " in upper and " TYPE " in upper:
        return LockImpact(
            operation=OperationClass.TABLE_REWRITE,
            lock_mode="AccessExclusiveLock",
            blocks_reads=True,
            blocks_writes=True,
            rewrites_table=True,
            estimate_ms=_estimate_ms(rows, REWRITE_ROWS_PER_SEC),
            confidence=Confidence.MEDIUM,
            reasoning=(
                "Changing a column type rewrites every row and rebuilds every index on "
                "the table, holding AccessExclusiveLock throughout — reads and writes "
                "both block for the full duration."
            ),
            safe_alternative=(
                "Add a new column of the target type, backfill in batches, sync with a "
                "trigger, swap the names, then drop the old column."
            ),
        )

    if " SET NOT NULL" in upper:
        return LockImpact(
            operation=OperationClass.TABLE_SCAN,
            lock_mode="AccessExclusiveLock",
            blocks_reads=True,
            blocks_writes=True,
            rewrites_table=False,
            estimate_ms=_estimate_ms(rows, SCAN_ROWS_PER_SEC),
            confidence=Confidence.MEDIUM,
            reasoning=(
                "SET NOT NULL scans the whole table to verify no NULLs exist, holding "
                "AccessExclusiveLock for the scan. No rewrite, so faster than a type "
                "change, but still proportional to table size."
            ),
            safe_alternative=(
                "On PG12+: add a CHECK (col IS NOT NULL) constraint as NOT VALID, run "
                "VALIDATE CONSTRAINT (which takes only a weak lock), then SET NOT NULL — "
                "it recognises the validated constraint and skips the scan."
            ),
        )

    if " DROP COLUMN " in upper:
        return LockImpact(
            operation=OperationClass.METADATA_ONLY,
            lock_mode="AccessExclusiveLock",
            blocks_reads=True,
            blocks_writes=True,
            rewrites_table=False,
            estimate_ms=1,
            confidence=Confidence.HIGH,
            reasoning=(
                "DROP COLUMN only marks the column dropped in the catalog; the data is "
                "reclaimed lazily by VACUUM, so the lock is held briefly. The danger "
                "here is data loss, not lock duration."
            ),
        )

    if " RENAME " in upper:
        return LockImpact(
            operation=OperationClass.METADATA_ONLY,
            lock_mode="AccessExclusiveLock",
            blocks_reads=True,
            blocks_writes=True,
            rewrites_table=False,
            estimate_ms=1,
            confidence=Confidence.HIGH,
            reasoning="A rename is a catalog-only change held for microseconds. Note it "
            "breaks any application still referring to the old name.",
        )

    if " SET DEFAULT" in upper or " DROP DEFAULT" in upper:
        return LockImpact(
            operation=OperationClass.METADATA_ONLY,
            lock_mode="AccessExclusiveLock",
            blocks_reads=True,
            blocks_writes=True,
            rewrites_table=False,
            estimate_ms=1,
            confidence=Confidence.HIGH,
            reasoning="Changing a column default affects future inserts only; existing "
            "rows are untouched, so there is no scan or rewrite.",
        )

    return LockImpact(
        operation=OperationClass.UNKNOWN,
        lock_mode="AccessExclusiveLock",
        blocks_reads=True,
        blocks_writes=True,
        rewrites_table=False,
        estimate_ms=None,
        confidence=Confidence.LOW,
        reasoning=(
            "This ALTER TABLE variant is not in the known-pattern library. ALTER TABLE "
            "defaults to AccessExclusiveLock, so assume reads and writes both block for "
            "an unknown duration."
        ),
    )
