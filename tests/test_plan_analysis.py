"""Unit tests for plan parsing and verdict rules, over synthetic plan JSON.

Synthetic rather than live plans here on purpose: these assert exact arithmetic (loop
multiplication, self-time subtraction, parallel handling) which needs plans with known
numbers. `test_explain.py` covers the same rules against plans Postgres actually
produced, so both "the maths is right" and "the maths matches reality" are checked.
"""

from __future__ import annotations

from typing import Any

from pgops.plan_analysis import VerdictKind, analyze, parse_plan


def plan(node: dict[str, Any], **meta: Any) -> list[dict[str, Any]]:
    return [{"Plan": node, **meta}]


def seq_scan(**overrides: Any) -> dict[str, Any]:
    node = {
        "Node Type": "Seq Scan",
        "Relation Name": "orders",
        "Plan Rows": 1000,
        "Actual Rows": 1000,
        "Actual Loops": 1,
        "Actual Total Time": 100.0,
    }
    node.update(overrides)
    return node


def test_rows_multiply_by_loops() -> None:
    root, _ = parse_plan(plan(seq_scan(**{"Actual Rows": 80_000, "Actual Loops": 3})))
    assert root.total_rows == 240_000


def test_time_multiplies_by_loops_when_sequential() -> None:
    """Inner side of a nested loop: 1000 iterations of 2ms really is 2 seconds."""
    root, _ = parse_plan(plan(seq_scan(**{"Actual Loops": 1000, "Actual Total Time": 2.0})))
    assert root.total_time_ms == 2000.0


def test_time_does_not_multiply_for_parallel_workers() -> None:
    """Regression: workers under a Gather run concurrently, so their times overlap.

    Multiplying here produced "5180ms of 2400ms total (216%)" against a real database.
    """
    tree = plan(
        {
            "Node Type": "Gather",
            "Plan Rows": 3000,
            "Actual Rows": 3000,
            "Actual Loops": 1,
            "Actual Total Time": 1700.0,
            "Plans": [seq_scan(**{"Actual Loops": 3, "Actual Total Time": 1700.0})],
        }
    )
    root, _ = parse_plan(tree)
    worker_scan = root.children[0]
    assert worker_scan.parallel is True
    assert worker_scan.total_time_ms == 1700.0  # not 5100
    # rows still sum across workers
    assert worker_scan.total_rows == 3000.0


def test_self_time_excludes_children() -> None:
    tree = plan(
        {
            "Node Type": "Sort",
            "Plan Rows": 100,
            "Actual Rows": 100,
            "Actual Loops": 1,
            "Actual Total Time": 500.0,
            "Plans": [seq_scan(**{"Actual Total Time": 400.0})],
        }
    )
    root, _ = parse_plan(tree)
    assert root.self_time_ms == 100.0


def test_seq_scan_on_small_table_is_not_flagged() -> None:
    """A sequential scan of a small table is the correct plan — flagging it would train
    the reader to ignore the verdict list."""
    root, _ = parse_plan(plan(seq_scan(**{"Actual Rows": 50, "Plan Rows": 50})))
    kinds = {v.kind for v in analyze(root)}
    assert VerdictKind.SEQ_SCAN_LARGE_TABLE not in kinds


def test_seq_scan_on_large_table_is_flagged() -> None:
    root, _ = parse_plan(plan(seq_scan(**{"Actual Rows": 500_000, "Plan Rows": 500_000})))
    kinds = {v.kind for v in analyze(root)}
    assert VerdictKind.SEQ_SCAN_LARGE_TABLE in kinds


def test_expensive_filter_flagged_when_most_rows_discarded() -> None:
    root, _ = parse_plan(
        plan(
            seq_scan(
                **{
                    "Actual Rows": 10,
                    "Plan Rows": 10,
                    "Rows Removed by Filter": 999_990,
                    "Filter": "(status = 'rare'::text)",
                }
            )
        )
    )
    verdicts = {v.kind: v for v in analyze(root)}
    assert VerdictKind.EXPENSIVE_FILTER in verdicts
    evidence = verdicts[VerdictKind.EXPENSIVE_FILTER].evidence
    # never claim 100% while rows survived — that reads as "returned nothing"
    assert ">99.9%" in evidence
    assert "999,990 of 1,000,000" in evidence


def test_total_discard_reports_exactly_100_percent() -> None:
    root, _ = parse_plan(
        plan(seq_scan(**{"Actual Rows": 0, "Plan Rows": 0, "Rows Removed by Filter": 1_000_000}))
    )
    verdicts = {v.kind: v for v in analyze(root)}
    assert "100.0%" in verdicts[VerdictKind.EXPENSIVE_FILTER].evidence


def test_selective_filter_not_flagged_as_expensive() -> None:
    root, _ = parse_plan(
        plan(seq_scan(**{"Actual Rows": 90_000, "Rows Removed by Filter": 10_000}))
    )
    kinds = {v.kind for v in analyze(root)}
    assert VerdictKind.EXPENSIVE_FILTER not in kinds


def test_sort_spill_flagged() -> None:
    root, _ = parse_plan(
        plan(
            {
                "Node Type": "Sort",
                "Plan Rows": 100,
                "Actual Rows": 100,
                "Actual Loops": 1,
                "Actual Total Time": 10.0,
                "Sort Method": "external merge",
                "Sort Space Type": "Disk",
                "Sort Space Used": 4096,
            }
        )
    )
    verdicts = {v.kind: v for v in analyze(root)}
    assert VerdictKind.SORT_SPILL in verdicts
    assert "4,096 kB" in verdicts[VerdictKind.SORT_SPILL].evidence


def test_in_memory_sort_not_flagged() -> None:
    root, _ = parse_plan(
        plan(
            {
                "Node Type": "Sort",
                "Plan Rows": 100,
                "Actual Rows": 100,
                "Actual Loops": 1,
                "Actual Total Time": 10.0,
                "Sort Method": "quicksort",
                "Sort Space Type": "Memory",
                "Sort Space Used": 25,
            }
        )
    )
    assert VerdictKind.SORT_SPILL not in {v.kind for v in analyze(root)}


def test_estimate_divergence_underestimate() -> None:
    root, _ = parse_plan(plan(seq_scan(**{"Plan Rows": 100, "Actual Rows": 100_000})))
    verdicts = {v.kind: v for v in analyze(root)}
    assert VerdictKind.ESTIMATE_DIVERGENCE in verdicts
    assert "underestimated" in verdicts[VerdictKind.ESTIMATE_DIVERGENCE].evidence


def test_estimate_divergence_overestimate() -> None:
    root, _ = parse_plan(plan(seq_scan(**{"Plan Rows": 500_000, "Actual Rows": 100})))
    verdicts = {v.kind: v for v in analyze(root)}
    assert VerdictKind.ESTIMATE_DIVERGENCE in verdicts
    assert "overestimated" in verdicts[VerdictKind.ESTIMATE_DIVERGENCE].evidence


def test_small_absolute_divergence_ignored() -> None:
    """1 row estimated vs 50 actual is a 50x ratio but changes no plan decision."""
    root, _ = parse_plan(plan(seq_scan(**{"Plan Rows": 1, "Actual Rows": 50})))
    assert VerdictKind.ESTIMATE_DIVERGENCE not in {v.kind for v in analyze(root)}


def test_accurate_estimate_not_flagged() -> None:
    root, _ = parse_plan(plan(seq_scan(**{"Plan Rows": 100_000, "Actual Rows": 105_000})))
    assert VerdictKind.ESTIMATE_DIVERGENCE not in {v.kind for v in analyze(root)}


def test_nested_loop_blowup_flagged() -> None:
    tree = plan(
        {
            "Node Type": "Nested Loop",
            "Plan Rows": 50_000,
            "Actual Rows": 50_000,
            "Actual Loops": 1,
            "Actual Total Time": 5000.0,
            "Plans": [
                seq_scan(**{"Actual Rows": 50_000, "Plan Rows": 50_000}),
                {
                    "Node Type": "Index Scan",
                    "Relation Name": "customers",
                    "Plan Rows": 1,
                    "Actual Rows": 1,
                    "Actual Loops": 50_000,
                    "Actual Total Time": 0.08,
                },
            ],
        }
    )
    root, _ = parse_plan(tree)
    verdicts = {v.kind: v for v in analyze(root)}
    assert VerdictKind.NESTED_LOOP_BLOWUP in verdicts
    assert "50,000 times" in verdicts[VerdictKind.NESTED_LOOP_BLOWUP].evidence


def test_reasonable_nested_loop_not_flagged() -> None:
    tree = plan(
        {
            "Node Type": "Nested Loop",
            "Plan Rows": 10,
            "Actual Rows": 10,
            "Actual Loops": 1,
            "Actual Total Time": 5.0,
            "Plans": [
                seq_scan(**{"Actual Rows": 10, "Plan Rows": 10}),
                {
                    "Node Type": "Index Scan",
                    "Relation Name": "customers",
                    "Plan Rows": 1,
                    "Actual Rows": 1,
                    "Actual Loops": 10,
                    "Actual Total Time": 0.05,
                },
            ],
        }
    )
    root, _ = parse_plan(tree)
    assert VerdictKind.NESTED_LOOP_BLOWUP not in {v.kind for v in analyze(root)}


def test_dominant_node_share_never_exceeds_total() -> None:
    tree = plan(
        {
            "Node Type": "Gather",
            "Plan Rows": 1000,
            "Actual Rows": 1000,
            "Actual Loops": 1,
            "Actual Total Time": 1000.0,
            "Plans": [seq_scan(**{"Actual Loops": 4, "Actual Total Time": 900.0})],
        }
    )
    root, _ = parse_plan(tree)
    for verdict in analyze(root):
        if verdict.kind is VerdictKind.DOMINANT_NODE:
            percent = int(verdict.evidence.split("(")[1].split("%")[0])
            assert percent <= 100, verdict.evidence


def test_plan_without_analyze_still_flags_large_seq_scan() -> None:
    """No ANALYZE means no actual values — the planner's own estimate is still enough
    to warn about a full-table scan."""
    root, _ = parse_plan(
        plan({"Node Type": "Seq Scan", "Relation Name": "orders", "Plan Rows": 500_000})
    )
    verdicts = analyze(root)
    kinds = {v.kind for v in verdicts}
    assert VerdictKind.SEQ_SCAN_LARGE_TABLE in kinds
    # ...but nothing that requires actual values
    assert VerdictKind.ESTIMATE_DIVERGENCE not in kinds
    assert VerdictKind.DOMINANT_NODE not in kinds


def test_verdicts_sorted_most_severe_first() -> None:
    root, _ = parse_plan(
        plan(
            seq_scan(
                **{
                    "Actual Rows": 10,
                    "Plan Rows": 10,
                    "Rows Removed by Filter": 999_990,
                    "Filter": "(status = 'rare'::text)",
                }
            )
        )
    )
    verdicts = analyze(root)
    severities = [v.severity.value for v in verdicts]
    assert severities == sorted(severities, key=["critical", "warning", "info"].index)


def test_meta_carries_timings() -> None:
    _, meta = parse_plan(plan(seq_scan(), **{"Planning Time": 1.5, "Execution Time": 42.0}))
    assert meta == {"planning_time_ms": 1.5, "execution_time_ms": 42.0}


def test_compact_tree_omits_raw_noise() -> None:
    root, _ = parse_plan(plan(seq_scan(**{"Shared Hit Blocks": 999, "Parallel Aware": False})))
    compact = root.to_dict()
    assert compact["node"] == "Seq Scan on orders"
    assert "Shared Hit Blocks" not in compact
