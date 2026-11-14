"""Tests for the change-detection probe planner."""

from functools import lru_cache

import pytest

from schedule.probe_planner import (
    ProbePlanningError,
    Source,
    build_probe_plan,
    render_plan,
    worst_gap,
)
from schedule.probe_planner import (
    _feasible,
    _normalize_windows,
    _PreparedSource,
)


HORIZON = 8
BUDGET = 6


def small_example_sources():
    # A's two windows can only be straddled with a gap of at least
    # 6 - 3 = 3, which structurally rules out every plan with worst gap 2.
    return [
        Source("A", [(0, 3), (6, 8)], max_unknown=3, group="g1"),
        Source("B", [(2, 8)], max_unknown=3, group="g1"),
        Source("C", [(3, 8)], max_unknown=3),
    ]


def brute_force_optimum(sources, horizon, budget, tolerance):
    """Exhaustive optimum via an independent token-by-token DP.

    ``tolerance`` is either a single cap applied to every source or a
    per-source sequence of caps.  State: (token, ages, mask, spent, peak).
    At the start of a token the age of a source is the gap a probe now
    would realise (token index when never probed).  Every subset of
    sources may probe at a token, subject to window membership, the
    shared-token group rule and the budget; probing resets the age to
    one.  The peak is updated at probe events and the terminal tail gaps
    are ``age - 1``.
    """
    points = [set(_normalize_windows(s.windows, horizon)) for s in sources]
    group_of = [s.group for s in sources]
    n = len(sources)
    all_sources = (1 << n) - 1
    if isinstance(tolerance, int):
        caps = (tolerance,) * n
    else:
        caps = tuple(tolerance)

    @lru_cache(maxsize=None)
    def best(token, ages, mask, spent, peak):
        if spent > budget:
            return None
        if token > horizon:
            if mask != all_sources or any(
                age - 1 > caps[i] for i, age in enumerate(ages)
            ):
                return None
            return max(peak, max(age - 1 for age in ages))
        if any(age > caps[i] for i, age in enumerate(ages)):
            return None
        outcomes = []
        for probe_mask in range(1 << n):
            chosen = tuple(i for i in range(n) if probe_mask & (1 << i))
            if any(token not in points[i] for i in chosen):
                continue
            groups_used = set()
            conflict = False
            for i in chosen:
                if group_of[i] is not None:
                    if group_of[i] in groups_used:
                        conflict = True
                        break
                    groups_used.add(group_of[i])
            if conflict:
                continue
            new_peak = max(peak, max((ages[i] for i in chosen), default=0))
            new_ages = tuple(
                1 if i in chosen else ages[i] + 1 for i in range(n)
            )
            result = best(
                token + 1,
                new_ages,
                mask | probe_mask,
                spent + len(chosen),
                new_peak,
            )
            if result is not None:
                outcomes.append(result)
        return min(outcomes) if outcomes else None

    return best(0, (0,) * n, 0, 0, 0)


def assert_plan_is_valid(plan, sources, horizon, budget):
    assert plan.within_budget
    assert plan.probe_count <= budget
    window_points = {
        source.name: set(_normalize_windows(source.windows, horizon))
        for source in sources
    }
    groups = {}
    for source in sources:
        tokens = plan.probes[source.name]
        assert tokens, f"{source.name} has no probes"
        assert set(tokens) <= window_points[source.name]
        assert worst_gap(tokens, horizon) <= source.max_unknown
        assert worst_gap(tokens, horizon) <= plan.worst_gap
        if source.group is not None:
            occupied = groups.setdefault(source.group, set())
            assert not occupied.intersection(tokens)
            occupied.update(tokens)


def test_small_example_worst_gap_is_three():
    sources = small_example_sources()
    plan = build_probe_plan(sources, HORIZON, BUDGET)
    assert plan.worst_gap == 3
    assert plan.probe_count == BUDGET
    assert_plan_is_valid(plan, sources, HORIZON, BUDGET)


def test_small_example_no_shorter_gap_exists():
    sources = small_example_sources()
    prepared = [_PreparedSource(source, HORIZON) for source in sources]

    assert _feasible(prepared, gap=2, budget=BUDGET) is None

    optimum = brute_force_optimum(sources, HORIZON, BUDGET, tolerance=3)
    assert optimum == 3


def test_small_example_concrete_table():
    plan = build_probe_plan(small_example_sources(), HORIZON, BUDGET)
    assert worst_gap(plan.probes["A"], HORIZON) <= 3
    assert worst_gap(plan.probes["B"], HORIZON) <= 3
    assert worst_gap(plan.probes["C"], HORIZON) <= 3
    assert not set(plan.probes["A"]).intersection(plan.probes["B"])
    print("\n" + render_plan(plan))


def test_shared_quota_cannot_share_a_token():
    sources = [
        Source("x", [(0, 4)], max_unknown=4, group="shared"),
        Source("y", [(0, 4)], max_unknown=4, group="shared"),
    ]
    plan = build_probe_plan(sources, horizon=4, budget=10)
    assert not set(plan.probes["x"]).intersection(plan.probes["y"])
    assert plan.worst_gap == 2
    assert _feasible(
        [_PreparedSource(source, 4) for source in sources], gap=1, budget=10
    ) is None


def test_budget_limits_probe_count():
    source = Source("s", [(0, 10)], max_unknown=10)
    plan_three = build_probe_plan([source], horizon=10, budget=3)
    assert plan_three.probe_count == 3
    assert plan_three.worst_gap == 3  # m probes make m+1 gaps; ceil(10/4)

    plan_two = build_probe_plan([source], horizon=10, budget=2)
    assert plan_two.probe_count == 2
    assert plan_two.worst_gap == 4  # ceil(10/3)


def test_inaccessible_gap_drives_the_worst_gap():
    source = Source("s", [(0, 2), (6, 8)], max_unknown=4)
    plan = build_probe_plan([source], horizon=8, budget=10)
    assert plan.probes[source.name] == (2, 6)
    assert plan.worst_gap == 4

    strict = Source("s", [(0, 2), (6, 8)], max_unknown=3)
    with pytest.raises(ProbePlanningError):
        build_probe_plan([strict], horizon=8, budget=10)


def test_window_boundaries_are_valid_tokens():
    source = Source("s", [(0, 0), (5, 5)], max_unknown=5)
    plan = build_probe_plan([source], horizon=5, budget=10)
    assert plan.probes[source.name] == (5,)
    assert plan.worst_gap == 5

    prepared = [_PreparedSource(source, 5)]
    assert _feasible(prepared, gap=4, budget=10) is None


def test_max_unknown_violation_raises():
    source = Source("s", [(0, 10)], max_unknown=2)
    with pytest.raises(ProbePlanningError):
        build_probe_plan([source], horizon=10, budget=2)


def test_budget_too_small_raises():
    source = Source("s", [(0, 10)], max_unknown=10)
    with pytest.raises(ProbePlanningError):
        build_probe_plan([source], horizon=10, budget=0)


def test_worst_gap_helper():
    assert worst_gap([0, 5], 5) == 5
    assert worst_gap([2, 6], 8) == 4
    assert worst_gap([0, 2, 4, 5], 5) == 2
    with pytest.raises(ValueError):
        worst_gap([], 5)


def test_invalid_inputs():
    with pytest.raises(ProbePlanningError):
        build_probe_plan([], horizon=5, budget=1)
    with pytest.raises(ProbePlanningError):
        build_probe_plan(
            [Source("a", [(0, 5)], 5), Source("a", [(0, 5)], 5)],
            horizon=5,
            budget=10,
        )
    with pytest.raises(ProbePlanningError):
        build_probe_plan(
            [Source("a", [(0, 5)], 5)], horizon=-1, budget=10
        )
    with pytest.raises(ProbePlanningError):
        build_probe_plan(
            [Source("a", [(0, 5)], 5)], horizon=5, budget=-1
        )
    with pytest.raises(ProbePlanningError):
        build_probe_plan(
            [Source("a", [(9, 5)], 5)], horizon=5, budget=10
        )


def test_random_instances_match_brute_force():
    import random

    rng = random.Random(20261114)
    for _ in range(250):
        horizon = rng.randint(0, 8)
        budget = rng.randint(0, 14)
        sources = []
        for index in range(rng.randint(1, 4)):
            window_count = rng.randint(1, 2)
            windows = []
            for _ in range(window_count):
                lo = rng.randint(0, horizon)
                hi = rng.randint(lo, horizon)
                windows.append((lo, hi))
            group = rng.choice([None, None, None, "g0", "g1", "g2"])
            sources.append(
                Source(
                    f"s{index}",
                    windows,
                    max_unknown=rng.randint(0, horizon + 1),
                    group=group,
                )
            )

        caps = [source.max_unknown for source in sources]
        optimum = brute_force_optimum(sources, horizon, budget, caps)
        if optimum is None:
            with pytest.raises(ProbePlanningError):
                build_probe_plan(sources, horizon, budget)
        else:
            plan = build_probe_plan(sources, horizon, budget)
            assert plan.worst_gap == optimum
            assert_plan_is_valid(plan, sources, horizon, budget)


if __name__ == "__main__":
    plan = build_probe_plan(small_example_sources(), HORIZON, BUDGET)
    print(render_plan(plan))
    prepared = [_PreparedSource(source, HORIZON) for source in small_example_sources()]
    print("feasible with worst gap 2?", _feasible(prepared, 2, BUDGET) is not None)
    print("feasible with worst gap 3?", _feasible(prepared, 3, BUDGET) is not None)
