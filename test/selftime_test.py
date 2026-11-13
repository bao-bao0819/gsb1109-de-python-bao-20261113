"""
Tests for exclusive (self) time computation from profiler task records.

Covers the cases where naive per-child subtraction breaks:
- concurrent / overlapping sibling calls (union, not sum),
- duplicate reports of the same task,
- children whose tail keeps running after the parent ends,
- records merely contained in time but with no parent link,
- traceability of every deducted time unit to a real child call.
"""
from __future__ import absolute_import

import random

import pytest

from selftime import (
    TaskRecord,
    compute_self_segments,
    compute_self_times,
    child_union_segments,
    coverage_breakdown,
)


def rec(task_id, start, end, parent_id=None):
    return TaskRecord(task_id, start, end, parent_id)


def test_single_task_self_time_is_full_duration():
    result = compute_self_times([rec("root", 0, 10)])
    assert result == {"root": 10}
    assert compute_self_segments([rec("root", 0, 10)]) == {"root": [(0, 10)]}


def test_nested_child_subtracted_from_both_sides():
    records = [rec("root", 0, 10), rec("child", 2, 8, "root")]
    assert compute_self_times(records) == {"root": 4, "child": 6}
    assert compute_self_segments(records)["root"] == [(0, 2), (8, 10)]
    assert child_union_segments(records)["root"] == [(2, 8)]


def test_sequential_children_are_fully_removed():
    records = [
        rec("root", 0, 10),
        rec("a", 1, 3, "root"),
        rec("b", 4, 6, "root"),
    ]
    assert compute_self_times(records)["root"] == 6
    assert child_union_segments(records)["root"] == [(1, 3), (4, 6)]


def test_concurrent_overlapping_siblings_counted_once_as_union():
    # Naive subtraction: 10 - 4 - 4 = 2. Union of [1,5] and [3,7] is [1,7],
    # length 6, so the real self time is 4.
    records = [
        rec("root", 0, 10),
        rec("a", 1, 5, "root"),
        rec("b", 3, 7, "root"),
    ]
    assert compute_self_times(records)["root"] == 4
    assert child_union_segments(records)["root"] == [(1, 7)]


def test_naive_sum_would_go_negative_but_self_time_never_does():
    # Three concurrent children of length 6 inside a parent of length 10:
    # naive subtraction yields 10 - 18 = -8. The union is [1, 9], length 8.
    records = [
        rec("root", 0, 10),
        rec("a", 1, 7, "root"),
        rec("b", 2, 8, "root"),
        rec("c", 3, 9, "root"),
    ]
    result = compute_self_times(records)
    assert result["root"] == 2
    assert all(value >= 0 for value in result.values())


def test_duplicate_report_of_same_child_is_counted_once():
    records = [
        rec("root", 0, 10),
        rec("child", 2, 6, "root"),
        rec("child", 2, 6, "root"),  # same span reported twice
    ]
    assert compute_self_times(records) == {"root": 6, "child": 4}
    assert child_union_segments(records)["root"] == [(2, 6)]


def test_duplicate_reports_merged_by_envelope():
    records = [
        rec("root", 0, 10),
        rec("child", 2, 5, "root"),
        rec("child", 4, 6, "root"),
    ]
    assert compute_self_times(records)["child"] == 4
    assert child_union_segments(records)["root"] == [(2, 6)]


def test_disjoint_duplicate_reports_leave_gap_as_self_time():
    # Same child id reported twice with a real gap between the reports.
    # The gap [5, 8) was never occupied by any child call, so it must remain
    # chargeable to the parent instead of being swallowed by an envelope.
    records = [
        rec("root", 0, 20),
        rec("child", 2, 5, "root"),
        rec("child", 8, 10, "root"),
    ]
    assert child_union_segments(records)["root"] == [(2, 5), (8, 10)]
    assert compute_self_times(records)["root"] == 15
    assert compute_self_segments(records)["root"] == [(0, 2), (5, 8), (10, 20)]


def test_task_own_disjoint_reports_skip_unreported_gap():
    records = [rec("t", 0, 2), rec("t", 6, 8)]
    assert compute_self_times(records) == {"t": 4}
    assert compute_self_segments(records)["t"] == [(0, 2), (6, 8)]


def test_child_tail_outliving_parent_is_clipped_not_negative():
    # Child [5, 15] keeps running after parent [0, 10] ends. Only [5, 10]
    # can be charged to the parent; the detached tail belongs to nobody here.
    records = [rec("root", 0, 10), rec("child", 5, 15, "root")]
    result = compute_self_times(records)
    assert result == {"root": 5, "child": 10}
    assert child_union_segments(records)["root"] == [(5, 10)]
    assert compute_self_segments(records)["root"] == [(0, 5)]


def test_child_starting_before_parent_clipped_to_parent_interval():
    records = [rec("root", 4, 10), rec("child", 0, 8, "root")]
    assert compute_self_times(records)["root"] == 2
    assert child_union_segments(records)["root"] == [(4, 8)]


def test_child_fully_outside_parent_covers_nothing():
    records = [rec("root", 0, 5), rec("child", 6, 9, "root")]
    assert compute_self_times(records)["root"] == 5
    assert child_union_segments(records)["root"] == []


def test_time_containment_without_parent_link_is_not_adopted():
    # "middle" sits inside root's interval but never names root as parent:
    # it must not be subtracted from root, however suggestive the timing is.
    records = [
        rec("root", 0, 10),
        rec("middle", 2, 8),  # no parent_id
        rec("leaf", 3, 5, "middle"),
    ]
    result = compute_self_times(records)
    assert result["root"] == 10
    assert result["middle"] == 4
    assert child_union_segments(records)["root"] == []
    assert child_union_segments(records)["middle"] == [(3, 5)]


def test_grandchild_is_not_directly_charged_to_grandparent():
    # Grandchild outlives its own parent (tail to 25). The grandparent only
    # loses the direct child's clipped interval [2, 10]; the grandchild tail
    # is not its debt.
    records = [
        rec("root", 0, 20),
        rec("child", 2, 10, "root"),
        rec("grand", 5, 25, "child"),
    ]
    result = compute_self_times(records)
    assert child_union_segments(records)["root"] == [(2, 10)]
    assert result["root"] == 12
    assert child_union_segments(records)["child"] == [(5, 10)]
    assert result["child"] == 3
    assert result["grand"] == 20


def test_unknown_parent_link_is_ignored_not_guessed():
    records = [rec("root", 0, 10), rec("orphan", 2, 8, "missing-parent")]
    result = compute_self_times(records)
    assert result["root"] == 10
    assert result["orphan"] == 6
    assert child_union_segments(records)["root"] == []


def test_dict_and_tuple_records_are_accepted():
    dict_records = [
        {"task_id": "root", "start": 0, "end": 10},
        {
            "task_id": "child",
            "start_time": 2,
            "end_time": 8,
            "parent_id": "root",
        },
    ]
    tuple_records = [("root", 0, 10), ("child", 2, 8, "root")]
    assert compute_self_times(dict_records) == compute_self_times(
        tuple_records
    )
    assert compute_self_times(tuple_records) == {"root": 4, "child": 6}


def test_float_timestamps_and_adjacent_children():
    records = [
        rec("root", 0.0, 1.0),
        rec("a", 0.1, 0.4, "root"),
        rec("b", 0.4, 0.9, "root"),  # touches a: half-open boundary
    ]
    result = compute_self_times(records)
    assert result["root"] == pytest.approx(0.2)
    assert child_union_segments(records)["root"] == [(0.1, 0.9)]


def test_coverage_breakdown_traces_every_deducted_unit_to_real_children():
    records = [
        rec("root", 0, 10),
        rec("a", 1, 5, "root"),
        rec("b", 3, 7, "root"),
    ]
    breakdown = coverage_breakdown(records)["root"]
    # [1, 3) only a is active; [3, 5) both; [5, 7) only b.
    assert breakdown == [
        (1, 3, ("a",)),
        (3, 5, ("a", "b")),
        (5, 7, ("b",)),
    ]
    covered_length = sum(end - start for start, end, _ in breakdown)
    assert covered_length == 6  # exactly the union length that was deducted
    direct_children = {"a", "b"}
    for start, end, covering in breakdown:
        assert covering  # never an empty witness set
        assert set(covering) <= direct_children


def test_breakdown_segments_tile_the_child_union():
    records = [
        rec("root", 0, 20),
        rec("a", 2, 6, "root"),
        rec("b", 5, 10, "root"),
        rec("c", 12, 18, "root"),
    ]
    breakdown = coverage_breakdown(records)["root"]
    union = child_union_segments(records)["root"]
    # Breakdown pieces tile each merged union segment end to end, including
    # gaps between separate union segments.
    for ustart, uend in union:
        pieces = [
            (start, end)
            for start, end, _ in breakdown
            if ustart <= start and end <= uend
        ]
        assert pieces
        assert pieces[0][0] == ustart
        assert pieces[-1][1] == uend
        cursor = ustart
        for start, end in pieces:
            assert start == cursor
            cursor = end


def test_inverted_interval_rejected():
    with pytest.raises(ValueError):
        compute_self_times([rec("bad", 5, 2)])


def test_conflicting_parents_for_same_task_rejected():
    with pytest.raises(ValueError):
        compute_self_times(
            [
                rec("x", 0, 10, "p1"),
                rec("x", 0, 10, "p2"),
            ]
        )


def test_record_without_id_rejected():
    with pytest.raises(ValueError):
        compute_self_times([{"start": 0, "end": 10}])


def test_fuzz_invariants_hold_under_random_hierarchies():
    rng = random.Random(20261114)
    for _ in range(200):
        records = []
        for task_id in range(rng.randint(1, 12)):
            start = rng.randint(0, 40)
            end = start + rng.randint(1, 20)
            parent_id = None
            if task_id and rng.random() < 0.7:
                parent_id = rng.randint(0, task_id - 1)
            records.append(rec(task_id, start, end, parent_id))
            # Random duplicate reports.
            if rng.random() < 0.3:
                records.append(rec(task_id, start, end, parent_id))

        times = compute_self_times(records)
        segments = compute_self_segments(records)
        unions = child_union_segments(records)
        breakdowns = coverage_breakdown(records)

        def merge(intervals):
            ordered = sorted(intervals)
            merged = []
            for start, end in ordered:
                if merged and start <= merged[-1][1]:
                    if end > merged[-1][1]:
                        merged[-1] = (merged[-1][0], end)
                else:
                    merged.append((start, end))
            return merged

        # Canonical reported-time union per task id, mirroring the module's
        # handling of duplicate reports.
        own_intervals = {}
        for record in records:
            own_intervals.setdefault(record.task_id, []).append(
                (record.start, record.end)
            )
        own = dict(
            (task_id, merge(intervals))
            for task_id, intervals in own_intervals.items()
        )
        # Explicit direct-child index: containment alone never creates a link.
        explicit_children = {}
        for record in records:
            if record.parent_id is not None and record.parent_id in own:
                explicit_children.setdefault(record.parent_id, set()).add(
                    record.task_id
                )

        for task_id, own_segments in own.items():
            duration = sum(end - start for start, end in own_segments)
            blocked = unions[task_id]
            # Union never escapes any segment of the task's own reported time.
            for bstart, bend in blocked:
                assert any(
                    ostart <= bstart and bend <= oend
                    for ostart, oend in own_segments
                )
            blocked_length = sum(
                bend - bstart for bstart, bend in blocked
            )
            # Invariant: self time + child union == inclusive duration,
            # and self time is never negative.
            assert times[task_id] >= 0
            assert times[task_id] + blocked_length == duration
            # Residual segments are disjoint from the blocked union and
            # together tile the full reported interval set.
            assert (
                sum(e - s for s, e in segments[task_id]) + blocked_length
                == duration
            )
            # Every deducted unit has at least one real direct-child witness
            # whose explicit parent is this task and whose reported interval
            # actually overlaps the charged segment.
            for bstart, bend, covering in breakdowns[task_id]:
                assert covering
                assert set(covering) <= explicit_children.get(task_id, set())
                for child_id in covering:
                    child_records = [
                        r for r in records if r.task_id == child_id
                    ]
                    assert any(
                        r.start < bend and r.end > bstart
                        for r in child_records
                    )
