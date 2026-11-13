"""Tests for the robust request planner in :mod:`schedule.planner`."""

import random

import pytest

from schedule.planner import (
    InfeasibleScheduleError,
    InvalidTaskError,
    Plan,
    Reservation,
    Task,
    Window,
    build_plan,
)


def make_task(task_id, duration_min, duration_max, deadline, value, credential,
              mandatory=False, category=None):
    return Task(
        task_id=task_id,
        duration_min=duration_min,
        duration_max=duration_max,
        deadline=deadline,
        value=value,
        credential=credential,
        mandatory=mandatory,
        category=category,
    )


def assert_reservation_inside_windows(plan):
    for reservation in plan.reservations:
        window = plan.windows[reservation.window_index]
        assert reservation.start >= max(plan.horizon_start, window.start)
        assert reservation.end <= window.end


def assert_no_credential_overlap(plan):
    by_credential = {}
    for reservation in plan.reservations:
        by_credential.setdefault(reservation.credential, []).append(reservation)
    for group in by_credential.values():
        ordered = sorted(group, key=lambda r: r.start)
        for earlier, later in zip(ordered, ordered[1:]):
            assert later.start >= earlier.end


def test_mandatory_tasks_reserve_worst_case_duration():
    windows = [Window(0, 10), Window(10, 20)]
    tasks = [
        make_task("A", 3, 5, 10, 10, "key-1", mandatory=True),
        make_task("B", 2, 4, 20, 8, "key-1", mandatory=True),
    ]
    plan = build_plan(tasks, windows)
    plan.validate()
    assert set(plan.guaranteed_task_ids) == {"A", "B"}
    for reservation in plan.reservations:
        task = plan.task_by_id(reservation.task_id)
        assert reservation.duration == pytest.approx(task.duration_max)
    assert_reservation_inside_windows(plan)
    assert_no_credential_overlap(plan)


def test_mandatory_task_too_large_for_every_window_is_infeasible():
    windows = [Window(0, 5), Window(10, 15)]
    tasks = [make_task("A", 4, 7, 15, 10, "key-1", mandatory=True)]
    with pytest.raises(InfeasibleScheduleError):
        build_plan(tasks, windows)


def test_mandatory_task_missing_deadline_window_is_infeasible():
    windows = [Window(0, 10), Window(20, 30)]
    tasks = [make_task("A", 2, 3, 12, 10, "key-1", mandatory=True)]
    # The only window that can finish before deadline 12 is [0, 10), and it fits.
    plan = build_plan(tasks, windows)
    assert plan.reservations[0].window_index == 0

    tight = [make_task("B", 9, 9, 12, 10, "key-1", mandatory=True)]
    plan_b = build_plan(tight, windows)
    assert plan_b.reservations[0].end == pytest.approx(9)

    impossible = [make_task("C", 11, 11, 25, 10, "key-1", mandatory=True)]
    with pytest.raises(InfeasibleScheduleError):
        build_plan(impossible, windows)


def test_credential_exclusivity_is_respected_under_pressure():
    windows = [Window(0, 6)]
    tasks = [
        make_task("A", 3, 3, 6, 10, "same", mandatory=True),
        make_task("B", 3, 3, 6, 10, "same", mandatory=True),
        make_task("C", 3, 3, 6, 10, "other", mandatory=True),
    ]
    plan = build_plan(tasks, windows)
    plan.validate()
    assert {r.task_id for r in plan.reservations} == {"A", "B", "C"}
    same_key = [r for r in plan.reservations if r.credential == "same"]
    assert len(same_key) == 2
    assert_no_credential_overlap(plan)


def test_overcommitted_credential_is_reported_infeasible():
    windows = [Window(0, 5)]
    tasks = [
        make_task("A", 3, 3, 5, 10, "same", mandatory=True),
        make_task("B", 3, 3, 5, 10, "same", mandatory=True),
    ]
    with pytest.raises(InfeasibleScheduleError):
        build_plan(tasks, windows)


def test_optional_guaranteed_then_best_effort_never_hurts_mandatory():
    windows = [Window(0, 10)]
    tasks = [
        make_task("M", 4, 5, 10, 100, "k", mandatory=True),
        # max fits in the 5 units left -> guaranteed
        make_task("G", 2, 3, 10, 40, "k"),
        # max does not fit in the 2 remaining units but min does -> best effort
        make_task("E", 2, 4, 10, 25, "k"),
    ]
    plan = build_plan(tasks, windows)
    plan.validate()
    assert "M" in plan.guaranteed_task_ids
    assert "G" in plan.guaranteed_task_ids
    assert plan.best_effort_task_ids == ["E"]
    reservation_e = next(r for r in plan.reservations if r.task_id == "E")
    assert reservation_e.duration == pytest.approx(2)
    assert_no_credential_overlap(plan)


def test_optional_tasks_are_dropped_when_no_slack_exists():
    windows = [Window(0, 6)]
    tasks = [
        make_task("M1", 3, 3, 6, 100, "k", mandatory=True),
        make_task("M2", 3, 3, 6, 100, "k", mandatory=True),
        make_task("O", 1, 1, 6, 50, "k"),
    ]
    plan = build_plan(tasks, windows)
    assert {r.task_id for r in plan.reservations} == {"M1", "M2"}
    assert plan.total_value == pytest.approx(200)


def test_higher_value_density_optional_task_is_preferred():
    windows = [Window(0, 5)]
    tasks = [
        make_task("low", 5, 5, 5, 5, "k"),
        make_task("high", 4, 4, 5, 40, "k"),
    ]
    plan = build_plan(tasks, windows)
    assert [r.task_id for r in plan.reservations] == ["high"]


def test_backtracking_finds_packing_that_naive_greedy_misses():
    # Two windows, one credential. Greedily filling the first window with the
    # long job leaves no room for the job that is locked to that window; the
    # planner must backtrack and move the flexible job to the second window.
    windows = [Window(0, 10), Window(10, 20)]
    tasks = [
        make_task("locked", 6, 6, 10, 10, "k", mandatory=True),
        make_task("flex-1", 5, 5, 20, 10, "k", mandatory=True),
        make_task("flex-2", 5, 5, 20, 10, "k", mandatory=True),
    ]
    plan = build_plan(tasks, windows)
    plan.validate()
    locked = next(r for r in plan.reservations if r.task_id == "locked")
    assert locked.window_index == 0
    flex_windows = {
        r.window_index for r in plan.reservations if r.task_id.startswith("flex")
    }
    assert flex_windows == {1}


def test_plan_validation_detects_manufactured_violations():
    windows = [Window(0, 10)]
    tasks = [make_task("A", 2, 4, 10, 10, "k", mandatory=True)]
    plan = build_plan(tasks, windows)
    # Tamper with the guaranteed reservation: mandatory task under-reserved.
    bad = plan.reservations[0]
    plan.reservations[0] = Reservation(
        task_id=bad.task_id,
        credential=bad.credential,
        window_index=bad.window_index,
        start=bad.start,
        end=bad.start + 1,
        guaranteed=True,
    )
    with pytest.raises(Exception):
        plan.validate()


def test_invalid_inputs_are_rejected():
    with pytest.raises(InvalidTaskError):
        make_task("A", 5, 3, 10, 10, "k").validate()
    with pytest.raises(InvalidTaskError):
        make_task("A", 0, 3, 10, 10, "k").validate()
    with pytest.raises(InvalidTaskError):
        make_task("A", 1, 3, 10, -1, "k").validate()
    with pytest.raises(InvalidTaskError):
        Window(5, 5).validate()

    windows = [Window(0, 10)]
    duplicate = [
        make_task("A", 1, 1, 10, 10, "k"),
        make_task("A", 1, 1, 10, 10, "k"),
    ]
    with pytest.raises(InvalidTaskError):
        build_plan(duplicate, windows)


def test_windows_are_clipped_to_horizon_start():
    windows = [Window(-5, 5), Window(5, 10)]
    tasks = [make_task("A", 3, 3, 10, 10, "k", mandatory=True)]
    plan = build_plan(tasks, windows, horizon_start=0)
    assert plan.windows[0].start == pytest.approx(0)
    plan.validate()


def _brute_force_feasible(mandatory_tasks, windows):
    """Exact feasibility check via exhaustive task/placement DFS."""

    def dfs(group, mask, intervals):
        if mask == 0:
            return True
        for task_index, task in enumerate(group):
            if not (mask >> task_index) & 1:
                continue
            for window_index, (lo, hi) in enumerate(intervals):
                end = lo + task.duration_max
                if end <= hi and end <= task.deadline:
                    remaining = list(intervals[:window_index])
                    # Only the unoccupied tail stays free; [lo, end) is booked.
                    if hi > end:
                        remaining.append((end, hi))
                    remaining.extend(intervals[window_index + 1 :])
                    if dfs(group, mask ^ (1 << task_index), tuple(remaining)):
                        return True
        return False

    by_credential = {}
    for task in mandatory_tasks:
        by_credential.setdefault(task.credential, []).append(task)

    initial = tuple((window.start, window.end) for window in windows)
    for group in by_credential.values():
        if not dfs(group, (1 << len(group)) - 1, initial):
            return False
    return True


def test_validation_rejects_best_effort_slot_before_a_guaranteed_one():
    windows = [Window(0, 12)]
    tasks = [
        make_task("M", 2, 4, 12, 100, "k", mandatory=True),
        make_task("E", 2, 6, 12, 10, "k"),
    ]
    plan = build_plan(tasks, windows)
    # Manufacture an unsafe layout: best-effort E first, guaranteed M after it.
    plan.reservations = [
        Reservation("E", "k", 0, 0, 2, guaranteed=False),
        Reservation("M", "k", 0, 2, 6, guaranteed=True),
    ]
    with pytest.raises(Exception):
        plan.validate()


def test_best_effort_slot_never_precedes_a_guaranteed_reservation():
    windows = [Window(0, 10), Window(10, 20)]
    tasks = [
        make_task("M1", 3, 4, 10, 100, "k", mandatory=True),
        make_task("M2", 3, 4, 20, 100, "k", mandatory=True),
        make_task("E", 2, 8, 20, 20, "k"),
    ]
    plan = build_plan(tasks, windows)
    plan.validate()
    if plan.best_effort_task_ids:
        for reservation in plan.reservations:
            if not reservation.guaranteed:
                later_guaranteed = [
                    other
                    for other in plan.reservations
                    if other.guaranteed
                    and other.window_index == reservation.window_index
                    and other.start >= reservation.start
                ]
                assert later_guaranteed == []


def test_greedy_fallback_still_produces_a_valid_guaranteed_plan(monkeypatch):
    import schedule.planner as planner_module

    # Force the exhaustive search to give up immediately; the EDF greedy
    # fallback must still return a sound guaranteed plan for an easy case.
    monkeypatch.setattr(planner_module, "SEARCH_NODE_BUDGET", 0)
    windows = [Window(0, 10), Window(10, 20)]
    tasks = [
        make_task("A", 3, 4, 10, 10, "k", mandatory=True),
        make_task("B", 2, 3, 20, 8, "k", mandatory=True),
    ]
    plan = build_plan(tasks, windows)
    plan.validate()
    assert set(plan.guaranteed_task_ids) == {"A", "B"}


def test_greedy_fallback_reports_infeasibility(monkeypatch):
    import schedule.planner as planner_module

    monkeypatch.setattr(planner_module, "SEARCH_NODE_BUDGET", 0)
    windows = [Window(0, 5)]
    tasks = [
        make_task("A", 3, 3, 5, 10, "k", mandatory=True),
        make_task("B", 3, 3, 5, 10, "k", mandatory=True),
    ]
    with pytest.raises(InfeasibleScheduleError):
        build_plan(tasks, windows)


def test_matches_brute_force_on_random_instances():
    rng = random.Random(20261114)
    for _ in range(300):
        window_count = rng.randint(1, 3)
        windows = []
        cursor = 0
        for _ in range(window_count):
            length = rng.randint(4, 9)
            windows.append(Window(cursor, cursor + length))
            cursor += length + rng.randint(0, 4)
        horizon_end = windows[-1].end

        tasks = []
        for index in range(rng.randint(1, 6)):
            duration_max = rng.randint(2, 6)
            duration_min = rng.randint(1, duration_max)
            deadline = rng.randint(duration_max, max(duration_max, horizon_end))
            tasks.append(
                make_task(
                    task_id="t%d" % index,
                    duration_min=duration_min,
                    duration_max=duration_max,
                    deadline=deadline,
                    value=rng.randint(1, 20),
                    credential="c%d" % rng.randint(0, 1),
                    mandatory=True,
                )
            )

        expected = _brute_force_feasible(tasks, windows)
        if expected:
            plan = build_plan(tasks, windows)
            assert isinstance(plan, Plan)
            plan.validate()
        else:
            with pytest.raises(InfeasibleScheduleError):
                build_plan(tasks, windows)
