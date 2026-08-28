"""db.health — one snapshot query per finding category, each annotated with a
severity + plain-language explanation (TOOLS.md) so an agent can act without also
knowing what a healthy `pg_stat_database` row looks like.

Thresholds are conservative defaults tuned for judgment calls, not hard science —
documented inline per finding so they're easy to retune per deployment size.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pgops.connections import ConnectionManager
from pgops.serialize import serialize_record, serialize_value

Severity = Literal["ok", "info", "warning", "critical"]

# `backend_type = 'client backend'` is the important clause. Without it this counts the
# checkpointer, the walwriter, the background writer, the autovacuum launcher and the
# logical replication launcher — none of which are connections, none of which consume a
# max_connections slot, and all of which report a NULL state. On an idle Postgres 16
# that produced "5 active backend connection(s)" with every one of them bucketed as
# "unknown", when exactly one client was attached.
#
# pgops's own backend is deliberately *not* excluded. The question this finding answers
# is how much max_connections headroom is left, and the observing connection occupies a
# slot like any other; filtering it out reported "0 client connections" from a session
# that was itself connected.
_CONNECTIONS_SQL = """
SELECT coalesce(state, 'unknown') AS state, count(*) AS n
FROM pg_stat_activity
WHERE backend_type = 'client backend'
GROUP BY 1
"""

# Connection exhaustion is an outage, and it arrives without warning: everything is fine
# until max_connections is reached and then nothing can connect at all — including the
# session an operator would use to investigate. Reporting the count alone leaves the
# reader to find the ceiling themselves, so the finding carries the headroom.
_MAX_CONNECTIONS_SQL = "SELECT setting::int FROM pg_settings WHERE name = 'max_connections'"

_CACHE_HIT_SQL = """
SELECT
    sum(blks_hit)::float8 / NULLIF(sum(blks_hit) + sum(blks_read), 0) AS ratio
FROM pg_stat_database
"""

_DEAD_TUPLES_SQL = """
SELECT relname, n_live_tup, n_dead_tup,
       round(100.0 * n_dead_tup / NULLIF(n_live_tup + n_dead_tup, 0), 1) AS dead_pct
FROM pg_stat_user_tables
WHERE n_dead_tup > 0
ORDER BY n_dead_tup DESC
LIMIT 10
"""

# Table bloat estimate. There is no exact bloat figure available without scanning every
# page (pgstattuple does that, but it is an extension and it is expensive on a large
# table), so this compares an estimate of the space live rows *should* occupy against
# the actual relation size.
#
# Getting that estimate right requires counting what a row costs on disk, not just what
# its data costs. An earlier version used `reltuples × sum(avg_width)` alone and was
# wrong on every narrow table, always in the alarming direction. Measured against a
# freshly loaded, never-updated table of 600k rows:
#
#     pgstattuple: dead_tuple_percent 0.00, free_percent 0.05   <- zero bloat
#     this tool:   "critical: order_items is roughly 55% bloat"
#
# sum(avg_width) for that table is 29 bytes; the real per-row cost is 52. Postgres adds
# a 23-byte tuple header MAXALIGNed to 24, plus a 4-byte line pointer in the page, and
# every page spends 24 bytes on its own header. Omitting all three understates live
# bytes by roughly 45% on narrow rows and reports the entire shortfall as reclaimable
# space — which pushes an agent toward VACUUM FULL, an AccessExclusiveLock that rewrites
# the table, on a table with nothing to reclaim.
#
# What remains unmodelled is per-column alignment padding, which avg_width cannot see.
# That errs toward *under*-reporting bloat, which is the right direction for a finding
# whose suggested remedy takes an exclusive lock.
_TUPLE_OVERHEAD_BYTES = 28  # 24-byte MAXALIGNed header + 4-byte line pointer
_PAGE_USABLE_BYTES = 8192 - 24  # page size less its header

_BLOAT_SQL = f"""
SELECT
    c.relname AS table_name,
    pg_relation_size(c.oid) AS actual_bytes,
    (ceil(
        (c.reltuples * ((SELECT sum(avg_width) FROM pg_stats s
                         WHERE s.schemaname = 'public' AND s.tablename = c.relname)
                        + {_TUPLE_OVERHEAD_BYTES}))
        / {_PAGE_USABLE_BYTES}::float8
     ) * 8192)::bigint AS estimated_live_bytes
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'r'
  AND n.nspname = 'public'
  AND c.reltuples > 1000
  AND pg_relation_size(c.oid) > 1024 * 1024
"""

_LONG_RUNNING_SQL = """
SELECT pid, now() - query_start AS duration, state, left(query, 200) AS query
FROM pg_stat_activity
WHERE state = 'active'
  AND pid <> pg_backend_pid()
  AND now() - query_start > interval '5 seconds'
ORDER BY duration DESC
"""

# pg_blocking_pids() is Postgres's own answer to "who is blocking this backend" and is
# the right tool here. The hand-rolled alternative — self-joining pg_locks on
# locktype/database/relation — is the recipe that circulates on blogs, and it is subtly
# wrong: it misses lock types those columns don't identify (tuple, transactionid,
# virtualxid, advisory), and it reports false positives for lock modes that don't
# actually conflict. pg_blocking_pids consults the real lock manager, including
# conflict-mode rules and parallel-worker leaders.
_WAITING_LOCKS_SQL = """
SELECT
    blocked.pid AS blocked_pid,
    left(blocked.query, 200) AS blocked_query,
    now() - blocked.query_start AS blocked_for,
    pg_blocking_pids(blocked.pid) AS blocking_pids
FROM pg_stat_activity blocked
WHERE cardinality(pg_blocking_pids(blocked.pid)) > 0
"""


@dataclass(slots=True)
class Finding:
    category: str
    severity: Severity
    summary: str
    detail: Any = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "category": self.category,
            "severity": self.severity,
            "summary": self.summary,
        }
        if self.detail is not None:
            d["detail"] = self.detail
        return d


@dataclass(slots=True)
class HealthReport:
    findings: list[Finding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"findings": [f.to_dict() for f in self.findings]}


async def db_health(conn_manager: ConnectionManager) -> HealthReport:
    findings: list[Finding] = []
    async with conn_manager.acquire_readonly() as conn:
        conn_rows = await conn.fetch(_CONNECTIONS_SQL)
        by_state = {r["state"]: r["n"] for r in conn_rows}
        total = sum(by_state.values())
        max_conns = await conn.fetchval(_MAX_CONNECTIONS_SQL)
        used_pct = total / max_conns if max_conns else 0.0
        conn_severity: Severity = (
            "critical" if used_pct >= 0.90 else ("warning" if used_pct >= 0.75 else "info")
        )
        findings.append(
            Finding(
                "connections",
                conn_severity,
                f"{total} client connection(s) of {max_conns} max ({used_pct:.0%} used)",
                {
                    "by_state": by_state,
                    "total": total,
                    "max_connections": max_conns,
                    "used_pct": round(used_pct * 100, 1),
                    "note": "background workers excluded (they consume no "
                    "max_connections slot); pgops's own pool connections are "
                    "included, because they do",
                },
            )
        )

        ratio = await conn.fetchval(_CACHE_HIT_SQL)
        if ratio is not None:
            # below ~0.99 is the conventional "look into shared_buffers / working set
            # size" line for an OLTP workload; not a hard threshold, just a nudge.
            severity: Severity = (
                "ok" if ratio >= 0.99 else ("warning" if ratio >= 0.90 else "critical")
            )
            findings.append(
                Finding(
                    "cache_hit_ratio",
                    severity,
                    f"buffer cache hit ratio {ratio:.4f}",
                    {"ratio": round(ratio, 4)},
                )
            )

        dead = await conn.fetch(_DEAD_TUPLES_SQL)
        if dead:
            worst = dead[0]
            severity = "warning" if (worst["dead_pct"] or 0) > 20 else "info"
            findings.append(
                Finding(
                    "dead_tuples",
                    severity,
                    f"{worst['relname']} has {worst['dead_pct']}% dead tuples "
                    f"({worst['n_dead_tup']} of {worst['n_live_tup'] + worst['n_dead_tup']})",
                    # serialize_record, not dict(): dead_pct is a Postgres numeric and
                    # arrives as a Decimal, which json.dumps cannot encode.
                    [serialize_record(r) for r in dead],
                )
            )

        for row in await conn.fetch(_BLOAT_SQL):
            live = row["estimated_live_bytes"]
            actual = row["actual_bytes"]
            if not live or live <= 0 or actual <= live:
                continue
            wasted = actual - live
            waste_pct = wasted / actual
            # 30% is where a VACUUM FULL / pg_repack starts being worth its disruption.
            if waste_pct < 0.30:
                continue
            findings.append(
                Finding(
                    "bloat",
                    "warning" if waste_pct < 0.5 else "critical",
                    f"{row['table_name']} is roughly {waste_pct:.0%} bloat "
                    f"(~{wasted // (1024 * 1024)} MB reclaimable) — estimate, not exact",
                    {
                        "table": row["table_name"],
                        "actual_bytes": actual,
                        "estimated_live_bytes": live,
                        "estimated_waste_pct": round(waste_pct * 100, 1),
                        "note": "estimated from reltuples × (avg row width + tuple "
                        "overhead), rounded up to whole pages; per-column alignment "
                        "padding is not modelled, so this reads low rather than high. "
                        "Confirm with pgstattuple before taking an exclusive lock.",
                    },
                )
            )

        long_running = await conn.fetch(_LONG_RUNNING_SQL)
        if long_running:
            findings.append(
                Finding(
                    "long_running_queries",
                    "warning",
                    f"{len(long_running)} quer(ies) running longer than 5s",
                    [
                        {
                            "pid": r["pid"],
                            "duration_s": serialize_value(r["duration"]),
                            "state": r["state"],
                            "query": r["query"],
                        }
                        for r in long_running
                    ],
                )
            )

        waiting = await conn.fetch(_WAITING_LOCKS_SQL)
        if waiting:
            findings.append(
                Finding(
                    "waiting_locks",
                    "critical",
                    f"{len(waiting)} session(s) blocked waiting on a lock",
                    [
                        {
                            "blocked_pid": r["blocked_pid"],
                            "blocked_query": r["blocked_query"],
                            "blocked_for_s": serialize_value(r["blocked_for"]),
                            "blocking_pids": list(r["blocking_pids"]),
                        }
                        for r in waiting
                    ],
                )
            )

    if not any(
        f.category in {"dead_tuples", "long_running_queries", "waiting_locks"} for f in findings
    ):
        findings.append(
            Finding("overall", "ok", "no dead-tuple, long-query, or lock-wait issues found")
        )

    return HealthReport(findings=findings)
