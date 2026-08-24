"""index.advise — index recommendations from catalog state and workload statistics.

Three independent findings, deliberately ordered by how confident we can be:

1. **Unused indexes** — `pg_stat_user_indexes.idx_scan = 0`. High confidence, and the
   cost is concrete: every index is written on every INSERT/UPDATE to its table and
   occupies disk. This is the recommendation most likely to be correct and most often
   overlooked.
2. **Redundant indexes** — an index whose column list is a strict prefix of another's.
   `(customer_id)` is redundant when `(customer_id, created_at)` exists, because the
   composite serves every query the single-column one does. Also high confidence, from
   catalog structure alone.
3. **Missing indexes** — inferred from `pg_stat_statements` plus per-table scan
   statistics. Genuinely heuristic, and labelled as such: we can see that a table is
   taking sequential scans under load, but not which column to index without a plan.
   The honest output is "here is the evidence, run query.explain on this statement" —
   not a fabricated `CREATE INDEX` that looks authoritative.

On pg_stat_statements being optional: it is an extension that may not be installed, and
requires `shared_preload_libraries`, which needs a restart. Its absence degrades this
tool to findings 1 and 2 with an explanatory note, rather than failing the call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import asyncpg

from pgops.connections import ConnectionManager
from pgops.serialize import serialize_record

_EXTENSION_PRESENT_SQL = """
SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements')
"""

# How long the counters this tool reasons about have actually been accumulating.
# NULL stats_reset means they were never explicitly reset, so the window starts at the
# last stats-file initialization — effectively "unknown, possibly very recent".
_STATS_AGE_SQL = """
SELECT stats_reset,
       EXTRACT(EPOCH FROM (now() - stats_reset)) AS seconds
FROM pg_stat_database
WHERE datname = current_database()
"""

# Below this, "zero scans" is far more likely to mean "we haven't been watching long
# enough" than "nothing uses this index". A weekly reporting query would need a week.
UNUSED_INDEX_MIN_OBSERVATION_S = 7 * 24 * 3600

# idx_scan counts index scans since the last stats reset; 0 means nothing has used it.
# Primary keys and unique constraints are excluded: they exist to enforce correctness,
# so "unused" is not a reason to drop them.
_UNUSED_INDEXES_SQL = """
SELECT
    s.relname AS table_name,
    s.indexrelname AS index_name,
    pg_relation_size(s.indexrelid) AS size_bytes,
    s.idx_scan AS scans
FROM pg_stat_user_indexes s
JOIN pg_index i ON i.indexrelid = s.indexrelid
WHERE s.idx_scan = 0
  AND NOT i.indisprimary
  AND NOT i.indisunique
ORDER BY pg_relation_size(s.indexrelid) DESC
LIMIT 20
"""

# indkey is an int2vector of column numbers in index order; comparing its text form
# lets us spot a prefix relationship without unnesting per index.
_INDEX_COLUMNS_SQL = """
SELECT
    t.relname AS table_name,
    ix.relname AS index_name,
    i.indkey::text AS column_ids,
    i.indisprimary AS is_primary,
    i.indisunique AS is_unique,
    pg_relation_size(i.indexrelid) AS size_bytes,
    pg_get_indexdef(i.indexrelid) AS definition
FROM pg_index i
JOIN pg_class ix ON ix.oid = i.indexrelid
JOIN pg_class t ON t.oid = i.indrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
WHERE n.nspname = 'public' AND i.indisvalid
ORDER BY t.relname, ix.relname
"""

# seq_scan on a large table with an existing index is the signature of queries whose
# filters no index covers.
_TABLE_SCAN_STATS_SQL = """
SELECT
    relname AS table_name,
    seq_scan,
    seq_tup_read,
    idx_scan,
    n_live_tup
FROM pg_stat_user_tables
WHERE seq_scan > 0 AND n_live_tup > 10000
ORDER BY seq_tup_read DESC
LIMIT 10
"""

_TOP_STATEMENTS_SQL = """
SELECT
    calls,
    round(total_exec_time::numeric, 2) AS total_ms,
    round(mean_exec_time::numeric, 2) AS mean_ms,
    rows,
    left(query, 300) AS query
FROM pg_stat_statements
WHERE query NOT ILIKE '%%pg_stat_statements%%'
  AND query NOT ILIKE 'EXPLAIN %%'
ORDER BY total_exec_time DESC
LIMIT $1
"""


@dataclass(slots=True)
class IndexAdvice:
    unused_indexes: list[dict[str, Any]] = field(default_factory=list)
    redundant_indexes: list[dict[str, Any]] = field(default_factory=list)
    scan_hotspots: list[dict[str, Any]] = field(default_factory=list)
    top_statements: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    stats_window: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stats_window": self.stats_window,
            "unused_indexes": self.unused_indexes,
            "redundant_indexes": self.redundant_indexes,
            "scan_hotspots": self.scan_hotspots,
            "top_statements": self.top_statements,
            "notes": self.notes,
        }


def _format_duration(seconds: float) -> str:
    if seconds >= 86400:
        return f"{seconds / 86400:.1f} days"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} hours"
    return f"{seconds / 60:.0f} minutes"


def _find_redundant(rows: list[asyncpg.Record]) -> list[dict[str, Any]]:
    """An index is redundant when its column list is a strict prefix of another's on the
    same table.

    Unique and primary-key indexes are never reported: `(email)` UNIQUE is not made
    redundant by `(email, created_at)` — the composite does not enforce the same
    constraint. Confusing "serves the same queries" with "enforces the same rule" is how
    an advisor talks someone into dropping their uniqueness guarantee.
    """
    findings: list[dict[str, Any]] = []
    by_table: dict[str, list[asyncpg.Record]] = {}
    for row in rows:
        by_table.setdefault(row["table_name"], []).append(row)

    for table, indexes in by_table.items():
        for candidate in indexes:
            if candidate["is_primary"] or candidate["is_unique"]:
                continue
            cand_cols = candidate["column_ids"].split()
            for other in indexes:
                if other["index_name"] == candidate["index_name"]:
                    continue
                other_cols = other["column_ids"].split()
                if len(other_cols) > len(cand_cols) and other_cols[: len(cand_cols)] == cand_cols:
                    findings.append(
                        {
                            "table": table,
                            "index": candidate["index_name"],
                            "size_bytes": candidate["size_bytes"],
                            "superseded_by": other["index_name"],
                            "reason": (
                                f"columns are a prefix of {other['index_name']}, which "
                                "serves the same queries"
                            ),
                            "suggestion": f"DROP INDEX {candidate['index_name']};",
                        }
                    )
                    break
    return findings


async def index_advise(conn_manager: ConnectionManager, limit: int = 10) -> IndexAdvice:
    advice = IndexAdvice()
    async with conn_manager.acquire_readonly() as conn:
        stats_row = await conn.fetchrow(_STATS_AGE_SQL)
        window_s: float | None = float(stats_row["seconds"]) if stats_row and stats_row["seconds"] else None
        window_trustworthy = window_s is not None and window_s >= UNUSED_INDEX_MIN_OBSERVATION_S
        advice.stats_window = {
            "stats_reset": stats_row["stats_reset"].isoformat()
            if stats_row and stats_row["stats_reset"]
            else None,
            "observed_for": _format_duration(window_s) if window_s else "unknown",
            "sufficient_for_unused_index_advice": window_trustworthy,
        }

        # Confidence, not just a finding. `idx_scan = 0` has two very different causes:
        # nothing uses this index, or the counters have barely been collecting. This
        # tool read a *stale* 0 for an index that a query had used seconds earlier and
        # would have said "DROP INDEX" about it — advice that is confidently wrong
        # destroys trust in every other finding the tool produces. So the observation
        # window is measured, reported, and gates whether a DROP is actually suggested.
        unused = await conn.fetch(_UNUSED_INDEXES_SQL)
        advice.unused_indexes = [
            {
                **serialize_record(row),
                "confidence": "high" if window_trustworthy else "low",
                "reason": (
                    f"zero scans recorded over {advice.stats_window['observed_for']} of "
                    "statistics; it still costs write throughput and disk"
                ),
                "suggestion": (
                    f"DROP INDEX {row['index_name']};"
                    if window_trustworthy
                    else (
                        f"do NOT drop {row['index_name']} yet — statistics have only been "
                        f"collected for {advice.stats_window['observed_for']}, which is too "
                        "short to conclude it is unused. Re-check after a full workload "
                        "cycle (including weekly/monthly jobs)."
                    )
                ),
            }
            for row in unused
        ]
        if unused and not window_trustworthy:
            advice.notes.append(
                "unused-index findings are LOW confidence: statistics have been collecting "
                f"for {advice.stats_window['observed_for']}, less than the "
                f"{UNUSED_INDEX_MIN_OBSERVATION_S // 86400} days needed to be confident an "
                "index is genuinely unused. Note also that pg_stat counters lag slightly, "
                "so an index used seconds ago can still read as zero."
            )

        advice.redundant_indexes = _find_redundant(list(await conn.fetch(_INDEX_COLUMNS_SQL)))

        hotspots = await conn.fetch(_TABLE_SCAN_STATS_SQL)
        advice.scan_hotspots = [
            {
                **serialize_record(row),
                "reason": (
                    f"{row['seq_scan']:,} sequential scans read {row['seq_tup_read']:,} rows "
                    f"from a table of ~{row['n_live_tup']:,}"
                ),
                "suggestion": (
                    "run query.explain on the queries hitting this table to see which "
                    "filter needs an index"
                ),
            }
            for row in hotspots
        ]

        if await conn.fetchval(_EXTENSION_PRESENT_SQL):
            try:
                statements = await conn.fetch(_TOP_STATEMENTS_SQL, limit)
                advice.top_statements = [serialize_record(row) for row in statements]
            except asyncpg.PostgresError as exc:
                # Installed but unreadable — column names differ across major versions
                # (total_time became total_exec_time in PG13). Degrade, don't fail.
                advice.notes.append(f"pg_stat_statements present but unreadable: {exc}")
        else:
            advice.notes.append(
                "pg_stat_statements is not installed, so workload-based advice is "
                "unavailable; findings are from catalog and table statistics only. "
                "Enable it with shared_preload_libraries = 'pg_stat_statements' "
                "(requires a restart) and CREATE EXTENSION pg_stat_statements."
            )

    if not any(
        [advice.unused_indexes, advice.redundant_indexes, advice.scan_hotspots]
    ):
        advice.notes.append("no unused or redundant indexes, and no sequential-scan hotspots")
    return advice
