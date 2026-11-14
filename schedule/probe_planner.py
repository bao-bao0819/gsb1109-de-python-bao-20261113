"""
Optimal scheduling of change-detection probes.

A data source only answers the question "has anything changed since the
last probe?" -- it never reports *when* a change happened.  If two
consecutive probes of a source happen at tokens ``p`` and ``q``, a change
occurring immediately after ``p`` is only discovered at ``q``, so the
worst-case undetected interval for that source is the gap between
consecutive probes.  The gaps from the horizon start to the first probe
and from the last probe to the horizon end count as well; the horizon
boundaries themselves are valid probe tokens.

Time is modelled as discrete, integer slots ("tokens").  Every probe of a
source must land inside one of the source's accessible windows.  Sources
that share a quota group may never occupy the same token, and the total
number of probes per planning period is bounded by a daily budget.

For a candidate worst-case gap ``D`` a single source is feasible exactly
when its accessible tokens contain a probe sequence ``p1 < ... < pm`` with

    ``p1 <= D``, ``p(i+1) - p(i) <= D`` and ``T - pm <= D``

where ``T`` is the horizon.  An inaccessible stretch longer than ``D``
makes the source infeasible automatically.  The rightmost-choice greedy
returns the smallest such sequence for sources without quota conflicts.

Sources inside a quota group are solved together with dynamic
programming: the state is the "age" of every group member (the gap a
probe now would realise) plus a mask of members probed at least once.
At every token the choices are "probe nobody" or "probe exactly one
member whose window contains the token".  Each member is bounded by
``min(D, max_unknown)``, so a source with a lenient tolerance may carry
a gap larger than the global optimum without violating it.  The
transition enforces distinct tokens, handles differing windows
naturally, minimises the number of probes and reconstructs the chosen
table via parent pointers.  The planner then binary searches the
smallest feasible global ``D`` between zero and the horizon.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "Source",
    "ProbePlan",
    "ProbePlanningError",
    "build_probe_plan",
    "worst_gap",
    "render_plan",
]


class ProbePlanningError(ValueError):
    """Raised when no plan can satisfy the hard window/quota constraints."""


@dataclass(frozen=True)
class Source:
    """A pollable data source.

    ``windows`` contains inclusive accessible intervals ``(lo, hi)`` of
    integer tokens.  ``max_unknown`` is the longest tolerated undetected
    interval.  Sources with the same non-empty ``group`` share a quota and
    may never be probed on the same token.
    """

    name: str
    windows: Sequence[Tuple[int, int]]
    max_unknown: int
    group: Optional[str] = None


@dataclass(frozen=True)
class ProbePlan:
    """A feasible probe table."""

    horizon: int
    probes: Mapping[str, Tuple[int, ...]]
    worst_gap: int
    budget: int

    @property
    def probe_count(self) -> int:
        return sum(len(tokens) for tokens in self.probes.values())

    @property
    def within_budget(self) -> bool:
        return self.probe_count <= self.budget


def _normalize_windows(
    windows: Sequence[Tuple[int, int]], horizon: int
) -> Tuple[int, ...]:
    """Return the sorted, de-duplicated accessible tokens of a source."""
    spans: List[Tuple[int, int]] = []
    for window in windows:
        lo, hi = int(window[0]), int(window[1])
        if lo > hi:
            raise ProbePlanningError(f"invalid window {(lo, hi)}: lo > hi")
        lo = max(lo, 0)
        hi = min(hi, horizon)
        if lo <= hi:
            spans.append((lo, hi))
    if not spans:
        raise ProbePlanningError(
            "source has no accessible token inside the horizon"
        )
    spans.sort()
    merged: List[Tuple[int, int]] = [spans[0]]
    for lo, hi in spans[1:]:
        last_lo, last_hi = merged[-1]
        if lo <= last_hi + 1:
            merged[-1] = (last_lo, max(last_hi, hi))
        else:
            merged.append((lo, hi))
    points: List[int] = []
    for lo, hi in merged:
        points.extend(range(lo, hi + 1))
    return tuple(points)


def _greedy_min_set(
    points: Tuple[int, ...], horizon: int, gap: int
) -> Optional[Tuple[int, ...]]:
    """Smallest probe sequence keeping every gap <= ``gap``.

    Always jump to the rightmost reachable token (the classic refuelling
    greedy), which minimises the number of probes.
    """
    starts = [token for token in points if token <= gap]
    if not starts:
        return None
    picked = [starts[-1]]
    while horizon - picked[-1] > gap:
        previous = picked[-1]
        reachable = [
            token for token in points if previous < token <= previous + gap
        ]
        if not reachable:
            return None
        picked.append(reachable[-1])
    return tuple(picked)


class _PreparedSource:
    def __init__(self, source: Source, horizon: int):
        if source.max_unknown < 0:
            raise ProbePlanningError(
                f"source {source.name!r} has a negative max_unknown"
            )
        self.name = source.name
        self.group = source.group
        self.max_unknown = source.max_unknown
        self.horizon = horizon
        self.points = _normalize_windows(source.windows, horizon)


def _group_feasibility(
    members: Sequence[_PreparedSource], gap: int, budget_left: int
) -> Optional[Tuple[int, List[Tuple[int, ...]]]]:
    """Cheapest feasible probe table for one quota group.

    Returns ``(probe_count, tokens_per_member)`` or ``None`` when the age
    constraint cannot be met or the cheapest table exceeds ``budget_left``.

    Each member has its own effective limit ``min(gap, max_unknown)``.  A
    DP state is ``(ages, mask)`` at the start of a token.  ``ages[i]`` is
    the gap that probing member ``i`` at this token would realise: the
    token index when it has never been probed, otherwise the distance to
    its last probe.  A probe is therefore legal exactly when
    ``ages[i] <= limit[i]``; afterwards the member's age becomes one
    while every other member ages by one.  ``mask`` records which members
    have been probed at least once.  At most one member probes per token,
    which is exactly the shared-quota rule; the terminal check uses
    ``age - 1`` for the tail gap back to the horizon boundary.
    """
    size = len(members)
    all_members = (1 << size) - 1
    limits = tuple(min(gap, member.max_unknown) for member in members)
    accessible = [set(member.points) for member in members]
    horizon = members[0].horizon

    dp: Dict[Tuple[Tuple[int, ...], int], int] = {
        (tuple(0 for _ in range(size)), 0): 0
    }
    history: List[
        Dict[
            Tuple[Tuple[int, ...], int],
            Tuple[Tuple[Tuple[int, ...], int], Optional[int]],
        ]
    ] = []

    for token in range(horizon + 1):
        next_dp = {}
        parents = {}

        def offer(state, candidate, previous, action) -> None:
            if candidate > budget_left:
                return
            ages, _ = state
            if any(age > limits[i] + 1 for i, age in enumerate(ages)):
                return
            if state not in next_dp or candidate < next_dp[state]:
                next_dp[state] = candidate
                parents[state] = (previous, action)

        for (ages, mask), cost in dp.items():
            aged = tuple(age + 1 for age in ages)
            offer((aged, mask), cost, (ages, mask), None)
            for index in range(size):
                if token in accessible[index] and ages[index] <= limits[index]:
                    reset = list(aged)
                    reset[index] = 1
                    offer(
                        (tuple(reset), mask | (1 << index)),
                        cost + 1,
                        (ages, mask),
                        index,
                    )
        dp = next_dp
        history.append(parents)

    best_state = None
    best_cost = None
    for (ages, mask), cost in dp.items():
        if mask != all_members:
            continue
        if any(age - 1 > limits[i] for i, age in enumerate(ages)):
            continue
        if best_cost is None or cost < best_cost:
            best_cost = cost
            best_state = (ages, mask)
    if best_state is None or best_cost > budget_left:
        return None

    tokens_per_member: List[List[int]] = [[] for _ in range(size)]
    state = best_state
    for token in range(horizon, -1, -1):
        previous, action = history[token][state]
        if action is not None:
            tokens_per_member[action].append(token)
        state = previous
    return best_cost, [tuple(sorted(tokens)) for tokens in tokens_per_member]


def _feasible(
    prepared: Sequence[_PreparedSource], gap: int, budget: int
) -> Optional[Dict[str, Tuple[int, ...]]]:
    assignment: Dict[str, Tuple[int, ...]] = {}
    spent = 0
    groups: Dict[str, List[_PreparedSource]] = {}
    for source in prepared:
        limit = min(gap, source.max_unknown)
        if source.group is None:
            min_set = _greedy_min_set(source.points, source.horizon, limit)
            if min_set is None:
                return None
            assignment[source.name] = min_set
            spent += len(min_set)
        else:
            groups.setdefault(source.group, []).append(source)
    if spent > budget:
        return None

    for members in groups.values():
        outcome = _group_feasibility(members, gap, budget - spent)
        if outcome is None:
            return None
        group_cost, token_lists = outcome
        spent += group_cost
        if spent > budget:
            return None
        for source, tokens in zip(members, token_lists):
            assignment[source.name] = tokens
    return assignment


def build_probe_plan(
    sources: Sequence[Source], horizon: int, budget: int
) -> ProbePlan:
    """Build the probe table with the shortest possible worst-case gap.

    Raises :class:`ProbePlanningError` when even the most lenient plan
    violates a source's ``max_unknown`` limit or the budget cannot fit any
    feasible table.
    """
    if horizon < 0:
        raise ProbePlanningError("horizon must be non-negative")
    if budget < 0:
        raise ProbePlanningError("budget must be non-negative")
    if not sources:
        raise ProbePlanningError("at least one source is required")
    names = [source.name for source in sources]
    if len(set(names)) != len(names):
        raise ProbePlanningError("source names must be unique")

    prepared = [_PreparedSource(source, horizon) for source in sources]

    decided: Dict[int, Optional[Dict[str, Tuple[int, ...]]]] = {}

    def decide(gap: int):
        if gap not in decided:
            decided[gap] = _feasible(prepared, gap, budget)
        return decided[gap]

    # No worst gap can exceed the horizon length, so feasibility at the
    # horizon is a sufficient upper bound for the binary search; states
    # are pruned per source at min(gap, max_unknown) + 1.
    if decide(horizon) is None:
        raise ProbePlanningError(
            "no feasible probe plan: a source cannot be polled often "
            "enough within its windows/budget"
        )

    lo, hi = 0, horizon
    while lo < hi:
        mid = (lo + hi) // 2
        if decide(mid) is not None:
            hi = mid
        else:
            lo = mid + 1

    probes = decide(lo)
    assert probes is not None
    return ProbePlan(
        horizon=horizon,
        probes={name: tuple(sorted(tokens)) for name, tokens in probes.items()},
        worst_gap=lo,
        budget=budget,
    )


def worst_gap(probes: Sequence[int], horizon: int) -> int:
    """Worst-case undetected interval of one source's probe tokens."""
    tokens = sorted(probes)
    if not tokens:
        raise ValueError("at least one probe token is required")
    result = max(tokens[0], horizon - tokens[-1])
    for previous, current in zip(tokens, tokens[1:]):
        result = max(result, current - previous)
    return result


def render_plan(plan: ProbePlan) -> str:
    """Render a human-readable probe table."""
    lines = [
        f"horizon=0..{plan.horizon} "
        f"worst_gap={plan.worst_gap} "
        f"probes={plan.probe_count}/{plan.budget}"
    ]
    by_token: Dict[int, List[str]] = {}
    for name, tokens in plan.probes.items():
        for token in tokens:
            by_token.setdefault(token, []).append(name)
    for token in sorted(by_token):
        lines.append(f"t={token:>4}: {', '.join(sorted(by_token[token]))}")
    return "\n".join(lines)
