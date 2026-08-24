"""EXPLAIN plan parsing and verdict rules (Phase 3).

Pure functions over the JSON Postgres emits for `EXPLAIN (FORMAT JSON)` — no database
access, so every rule is unit-testable against captured plans.

The two things this module exists to get right:

**Loops.** `Actual Rows`, `Actual Total Time` and `Plan Rows` in a plan node are all
*per loop*, not totals. A node executed as the inner side of a nested loop, or by three
parallel workers, reports `Actual Rows: 80000, Actual Loops: 3` — meaning 240,000 rows.
The naive parser compares `Plan Rows` against `Actual Rows` directly and reports wild
estimate divergence on every parallel plan in existence. Every rule here works from
`total_rows` / `total_time_ms`, which account for loops.

**Parallel loops are not sequential loops.** This is the subtlety that makes the above
insufficient, and the first version of this module got it wrong. `Actual Loops` counts
*iterations* under a Nested Loop but counts *concurrent workers* under a Gather. Rows
sum across workers either way — 3 workers × 400k rows really did read 1.2M rows. Wall
clock does not: three workers each taking 1700ms cost 5100ms of CPU but only ~1700ms of
elapsed time. Multiplying time by loops in a parallel subtree produced the verdict
"5180ms of 2400ms total (216%)" against the dev database — a number that is not only
wrong but obviously wrong. Nodes therefore carry `parallel`, set while walking down
through a Gather/Gather Merge, and time is multiplied by loops only when it isn't set.

**Self time.** A node's `Actual Total Time` includes all of its children. The node with
the highest total time is almost always the root, which tells you nothing. What
identifies the actual bottleneck is *self* time — total minus the children's totals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# --- thresholds -------------------------------------------------------------------
# Tuned to be quiet on healthy plans rather than maximally sensitive: a verdict list
# that flags something on every query trains the reader to ignore it. Each is stated
# as a named constant so the reasoning is visible and retuning is a one-line change.

# Below this, a sequential scan is usually the *correct* plan — Postgres reads a small
# table faster in one pass than via an index, and advising an index would be wrong.
SEQ_SCAN_MIN_ROWS = 10_000

# Fraction of scanned rows thrown away by a filter before it's worth an index. At 90%
# discard the scan is doing 10x the necessary I/O.
FILTER_DISCARD_RATIO = 0.9

# Estimate-vs-actual factor before the planner is considered meaningfully wrong. 10x is
# where plan *shape* choices (join order, nested loop vs hash) start going bad; smaller
# divergences rarely change which plan wins.
ESTIMATE_DIVERGENCE_FACTOR = 10.0
ESTIMATE_DIVERGENCE_MIN_ROWS = 1_000

# A nested loop whose inner side runs this many times is usually a join the planner
# should have hashed — it mis-estimated the outer side's cardinality.
NESTED_LOOP_LOOP_THRESHOLD = 10_000

# Share of total execution time a single node must own to be called the bottleneck.
DOMINANT_NODE_TIME_SHARE = 0.5


class VerdictKind(StrEnum):
    SEQ_SCAN_LARGE_TABLE = "seq_scan_large_table"
    EXPENSIVE_FILTER = "expensive_filter"
    ESTIMATE_DIVERGENCE = "estimate_divergence"
    SORT_SPILL = "sort_spill"
    NESTED_LOOP_BLOWUP = "nested_loop_blowup"
    DOMINANT_NODE = "dominant_node"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(slots=True)
class Verdict:
    kind: VerdictKind
    severity: Severity
    node: str
    evidence: str
    suggestion: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "severity": self.severity.value,
            "node": self.node,
            "evidence": self.evidence,
            "suggestion": self.suggestion,
        }


@dataclass(slots=True)
class PlanNode:
    """One node of the plan tree, with per-loop values already multiplied out."""

    node_type: str
    relation: str | None
    raw: dict[str, Any]
    children: list[PlanNode] = field(default_factory=list)

    # None when the plan was produced without ANALYZE (no actual values exist)
    actual_rows: float | None = None
    actual_loops: float = 1.0
    actual_total_time_ms: float | None = None
    planned_rows: float = 0.0
    # True when this node sits beneath a Gather/Gather Merge, i.e. its loops are
    # concurrent workers rather than sequential iterations.
    parallel: bool = False

    @property
    def label(self) -> str:
        return f"{self.node_type} on {self.relation}" if self.relation else self.node_type

    @property
    def total_rows(self) -> float | None:
        """Rows across all loops — what a human means by "how many rows".

        Multiplied by loops in both the parallel and sequential case: work split across
        workers is still work done.
        """
        if self.actual_rows is None:
            return None
        return self.actual_rows * self.actual_loops

    @property
    def total_planned_rows(self) -> float:
        return self.planned_rows * self.actual_loops

    @property
    def total_time_ms(self) -> float | None:
        """Elapsed time attributable to this node.

        Not multiplied by loops for parallel workers — they run concurrently, so their
        times overlap rather than add (see module docstring).
        """
        if self.actual_total_time_ms is None:
            return None
        if self.parallel:
            return self.actual_total_time_ms
        return self.actual_total_time_ms * self.actual_loops

    @property
    def self_time_ms(self) -> float | None:
        """Time in this node alone, excluding children (see module docstring)."""
        total = self.total_time_ms
        if total is None:
            return None
        child_time = 0.0
        for child in self.children:
            child_total = child.total_time_ms
            if child_total is not None:
                child_time += child_total
        return max(total - child_time, 0.0)

    def walk(self) -> list[PlanNode]:
        nodes = [self]
        for child in self.children:
            nodes.extend(child.walk())
        return nodes

    def to_dict(self) -> dict[str, Any]:
        """Compact tree for the tool response — the raw plan is enormous and most of
        it (per-worker buffer counters, cost internals) is noise to a caller deciding
        what to do next."""
        out: dict[str, Any] = {"node": self.label, "planned_rows": self.total_planned_rows}
        if self.total_rows is not None:
            out["actual_rows"] = self.total_rows
        if self.total_time_ms is not None:
            out["total_time_ms"] = round(self.total_time_ms, 2)
            self_ms = self.self_time_ms
            if self_ms is not None:
                out["self_time_ms"] = round(self_ms, 2)
        if self.actual_loops > 1:
            out["loops"] = self.actual_loops
        if self.children:
            out["children"] = [c.to_dict() for c in self.children]
        return out


def parse_plan(explain_json: list[dict[str, Any]] | dict[str, Any]) -> tuple[PlanNode, dict[str, Any]]:
    """Turn `EXPLAIN (FORMAT JSON)` output into a tree plus top-level timings.

    Postgres returns a single-element list; accepting a bare dict too keeps this usable
    on plans pasted from other sources.
    """
    root_obj = explain_json[0] if isinstance(explain_json, list) else explain_json
    node = _parse_node(root_obj["Plan"], parallel=False)
    meta = {
        "planning_time_ms": root_obj.get("Planning Time"),
        "execution_time_ms": root_obj.get("Execution Time"),
    }
    return node, {k: v for k, v in meta.items() if v is not None}


_GATHER_NODES = {"Gather", "Gather Merge"}


def _parse_node(obj: dict[str, Any], parallel: bool) -> PlanNode:
    node_type = obj.get("Node Type", "Unknown")
    node = PlanNode(
        node_type=node_type,
        relation=obj.get("Relation Name"),
        raw=obj,
        actual_rows=obj.get("Actual Rows"),
        actual_loops=float(obj.get("Actual Loops", 1) or 1),
        actual_total_time_ms=obj.get("Actual Total Time"),
        planned_rows=float(obj.get("Plan Rows", 0) or 0),
        parallel=parallel,
    )
    # The Gather itself runs once in the leader; everything below it is worker-parallel.
    child_parallel = parallel or node_type in _GATHER_NODES
    node.children = [_parse_node(child, child_parallel) for child in obj.get("Plans", [])]
    return node


# --- verdict rules ------------------------------------------------------------------


def analyze(root: PlanNode) -> list[Verdict]:
    verdicts: list[Verdict] = []
    for node in root.walk():
        verdicts.extend(_seq_scan_rules(node))
        verdicts.extend(_estimate_rules(node))
        verdicts.extend(_sort_rules(node))
        verdicts.extend(_nested_loop_rules(node))
    verdicts.extend(_bottleneck_rule(root))
    # most severe first so a caller reading only the first entry reads the worst one
    order = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}
    verdicts.sort(key=lambda v: order[v.severity])
    return verdicts


def _seq_scan_rules(node: PlanNode) -> list[Verdict]:
    if node.node_type != "Seq Scan":
        return []
    scanned = node.total_rows
    removed_per_loop = node.raw.get("Rows Removed by Filter")
    if scanned is None:
        # No ANALYZE: fall back to the planner's own estimate, which is still enough to
        # flag a large scan — just without the "rows removed" evidence.
        if node.total_planned_rows >= SEQ_SCAN_MIN_ROWS:
            return [
                Verdict(
                    VerdictKind.SEQ_SCAN_LARGE_TABLE,
                    Severity.WARNING,
                    node.label,
                    f"planner expects a sequential scan returning {node.total_planned_rows:,.0f} rows",
                    f"consider an index on the filtered column(s) of {node.relation}; "
                    "re-run with analyze=true for filter selectivity",
                )
            ]
        return []

    removed = (removed_per_loop or 0) * node.actual_loops
    examined = scanned + removed
    if examined < SEQ_SCAN_MIN_ROWS:
        # Small table: a sequential scan is the right plan, not a problem to report.
        return []

    verdicts = [
        Verdict(
            VerdictKind.SEQ_SCAN_LARGE_TABLE,
            Severity.WARNING,
            node.label,
            f"sequential scan examined {examined:,.0f} rows on {node.relation}",
            f"an index on {node.relation} covering the filter would avoid reading "
            "the whole table",
        )
    ]

    if removed and examined and (removed / examined) >= FILTER_DISCARD_RATIO:
        filter_expr = node.raw.get("Filter", "the filter")
        verdicts.append(
            Verdict(
                VerdictKind.EXPENSIVE_FILTER,
                Severity.CRITICAL,
                node.label,
                f"filter {filter_expr} discarded {removed:,.0f} of {examined:,.0f} rows "
                f"({_discard_percent(removed, examined)}) after reading them",
                f"index the column(s) in {filter_expr} so Postgres selects rows instead "
                "of reading and rejecting them",
            )
        )
    return verdicts


def _discard_percent(removed: float, examined: float) -> str:
    """Format a discard ratio without ever claiming 100% while rows survived.

    999,990 of 1,000,000 rounds to "100.0%" at one decimal place, which reads as "the
    filter returned nothing" — the opposite of the point being made.
    """
    ratio = removed / examined
    if ratio >= 0.999 and removed < examined:
        return ">99.9%"
    return f"{ratio:.1%}"


def _estimate_rules(node: PlanNode) -> list[Verdict]:
    actual = node.total_rows
    if actual is None:
        return []
    planned = node.total_planned_rows
    if max(actual, planned) < ESTIMATE_DIVERGENCE_MIN_ROWS:
        return []
    # +1 avoids a divide-by-zero and keeps the ratio meaningful when one side is 0
    ratio = (actual + 1) / (planned + 1)
    if ratio >= ESTIMATE_DIVERGENCE_FACTOR:
        direction, factor = "underestimated", ratio
    elif ratio <= 1 / ESTIMATE_DIVERGENCE_FACTOR:
        direction, factor = "overestimated", 1 / ratio
    else:
        return []
    return [
        Verdict(
            VerdictKind.ESTIMATE_DIVERGENCE,
            Severity.WARNING,
            node.label,
            f"planner {direction} rows by {factor:.0f}x "
            f"(estimated {planned:,.0f}, actual {actual:,.0f})",
            "run ANALYZE on the table; if it persists, the columns may be correlated — "
            "consider CREATE STATISTICS. Bad estimates make the planner pick bad join "
            "strategies, so this is often the root cause of an otherwise puzzling plan",
        )
    ]


def _sort_rules(node: PlanNode) -> list[Verdict]:
    # "Sort Space Type" is Memory or Disk; Disk means work_mem was insufficient and the
    # sort spilled, which is typically an order-of-magnitude slowdown.
    if node.raw.get("Sort Space Type") != "Disk":
        return []
    used_kb = node.raw.get("Sort Space Used", 0)
    method = node.raw.get("Sort Method", "external sort")
    return [
        Verdict(
            VerdictKind.SORT_SPILL,
            Severity.WARNING,
            node.label,
            f"sort spilled to disk ({method}, {used_kb:,} kB)",
            f"raise work_mem for this query (needs roughly {max(used_kb // 1024 + 1, 1)} MB), "
            "or add an index matching the ORDER BY so the sort disappears entirely",
        )
    ]


def _nested_loop_rules(node: PlanNode) -> list[Verdict]:
    if node.node_type != "Nested Loop" or not node.children:
        return []
    inner = node.children[-1]
    if inner.actual_loops < NESTED_LOOP_LOOP_THRESHOLD:
        return []
    return [
        Verdict(
            VerdictKind.NESTED_LOOP_BLOWUP,
            Severity.CRITICAL,
            node.label,
            f"inner side ({inner.label}) executed {inner.actual_loops:,.0f} times",
            "the planner expected few outer rows and chose a nested loop; fix the row "
            "estimate (ANALYZE / CREATE STATISTICS) so it picks a hash or merge join, "
            "or index the inner side's join key",
        )
    ]


def _bottleneck_rule(root: PlanNode) -> list[Verdict]:
    total = root.total_time_ms
    if total is None or total <= 0:
        return []
    nodes = root.walk()
    worst = max(nodes, key=lambda n: n.self_time_ms or 0.0)
    worst_self = worst.self_time_ms or 0.0
    if worst_self / total < DOMINANT_NODE_TIME_SHARE:
        return []
    return [
        Verdict(
            VerdictKind.DOMINANT_NODE,
            Severity.INFO,
            worst.label,
            f"{worst_self:.0f}ms of {total:.0f}ms total ({worst_self / total:.0%}) "
            "is spent in this node alone, excluding its children",
            "this is where the time actually goes — optimize here first",
        )
    ]
