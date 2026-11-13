"""
Robust request planning under short supply-side availability windows.

Domain model
------------
A supplier only accepts requests inside a few short :class:`Window`
periods. Every data fetch is non-preemptible: once started it runs
straight to completion inside one window (it cannot be paused, resumed
or moved to another window).

Each :class:`Task` has:

* a bounded duration ``duration_min <= actual <= duration_max``,
* a ``deadline`` by which the fetch must finish,
* a ``value`` and a ``credential``.

The same credential can only serve one fetch at a time.

A *guaranteed* plan reserves the worst-case duration
(``duration_max``) for every mandatory ("must-fetch") task. As long as
the real duration stays inside the declared bounds, every mandatory
task therefore completes no matter how the durations realize.

Optional tasks are scheduled with whatever is left over. They first
try to reserve ``duration_max`` (fully guaranteed as well); if that
does not fit but ``duration_min`` does, they get a best-effort slot
that is explicitly flagged as non-guaranteed. A best-effort slot is
only ever placed in the *tail* free fragment of a window (nothing is
booked after it in that window), so if the fetch runs long it can only
spill into idle time before the window closes or fail on its own
account; it can never delay a later guaranteed reservation. This is
the "use the slack from a request that finishes early" rule.

The module only depends on the Python standard library and works on a
simple numeric time grid (minutes since an arbitrary origin, e.g. the
start of the planning horizon).
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Hashable, List, Optional, Sequence, Set, Tuple

# Floating point comparisons use this tolerance when adding durations.
EPS = 1e-9

# Backtracking budget per credential while placing mandatory tasks.
SEARCH_NODE_BUDGET = 100_000

__all__ = [
    "Task",
    "Window",
    "Reservation",
    "Plan",
    "PlannerError",
    "InvalidTaskError",
    "InfeasibleScheduleError",
    "build_plan",
]


class PlannerError(Exception):
    """Base class for planner errors."""


class InvalidTaskError(PlannerError):
    """A task or window is malformed or internally inconsistent."""


class InfeasibleScheduleError(PlannerError):
    """The mandatory tasks cannot be guaranteed under any realization."""


@dataclass(frozen=True)
class Task:
    """A single data-fetch request.

    :param task_id: Unique, hashable identifier.
    :param duration_min: Lower bound on the fetch duration.
    :param duration_max: Upper bound on the fetch duration.
    :param deadline: Latest absolute time at which the fetch may finish.
    :param value: Value obtained when the fetch completes.
    :param credential: Credential used by the fetch; the same credential
        cannot run two fetches simultaneously.
    :param mandatory: When ``True`` the task belongs to a must-fetch
        category and is always reserved for ``duration_max``.
    :param category: Free-form label of the task's category.
    """

    task_id: Hashable
    duration_min: float
    duration_max: float
    deadline: float
    value: float
    credential: Hashable
    mandatory: bool = False
    category: Optional[str] = None

    def validate(self) -> None:
        if not isinstance(self.task_id, Hashable):
            raise InvalidTaskError("task_id must be hashable")
        if not isinstance(self.credential, Hashable):
            raise InvalidTaskError(
                "task %r credential must be hashable" % (self.task_id,)
            )
        if self.duration_min <= 0 or self.duration_max <= 0:
            raise InvalidTaskError(
                "task %r durations must be strictly positive" % (self.task_id,)
            )
        if self.duration_min > self.duration_max:
            raise InvalidTaskError(
                "task %r duration_min (%s) exceeds duration_max (%s)"
                % (self.task_id, self.duration_min, self.duration_max)
            )
        if self.value < 0:
            raise InvalidTaskError(
                "task %r value must be non-negative" % (self.task_id,)
            )
        if self.deadline < 0:
            raise InvalidTaskError(
                "task %r deadline must be on/after the horizon start"
                % (self.task_id,)
            )


@dataclass(frozen=True)
class Window:
    """A half-open availability interval ``[start, end)``."""

    start: float
    end: float

    def validate(self) -> None:
        if self.end <= self.start:
            raise InvalidTaskError(
                "window %s must be strictly longer than zero" % (self,)
            )


@dataclass(frozen=True)
class Reservation:
    """A concrete, non-preemptible slot reserved for one task.

    ``[start, end)`` lies entirely inside one window. ``guaranteed`` is
    ``True`` when the reserved length covers ``duration_max``; a
    best-effort reservation only covers ``duration_min``.
    """

    task_id: Hashable
    credential: Hashable
    window_index: int
    start: float
    end: float
    guaranteed: bool

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class Plan:
    """The result of :func:`build_plan`."""

    tasks: List[Task]
    windows: List[Window]
    horizon_start: float
    reservations: List[Reservation] = field(default_factory=list)

    def task_by_id(self, task_id: Hashable) -> Task:
        for task in self.tasks:
            if task.task_id == task_id:
                return task
        raise KeyError(task_id)

    @property
    def selected_tasks(self) -> List[Task]:
        return [self.task_by_id(r.task_id) for r in self.reservations]

    @property
    def mandatory_tasks(self) -> List[Task]:
        return [t for t in self.tasks if t.mandatory]

    @property
    def total_value(self) -> float:
        return sum(self.task_by_id(r.task_id).value for r in self.reservations)

    @property
    def guaranteed_value(self) -> float:
        return sum(
            self.task_by_id(r.task_id).value
            for r in self.reservations
            if r.guaranteed
        )

    @property
    def guaranteed_task_ids(self) -> List[Hashable]:
        return [r.task_id for r in self.reservations if r.guaranteed]

    @property
    def best_effort_task_ids(self) -> List[Hashable]:
        return [r.task_id for r in self.reservations if not r.guaranteed]

    def timeline(self) -> List[Reservation]:
        return sorted(
            self.reservations, key=lambda r: (r.start, r.end, str(r.task_id))
        )

    def free_intervals(self) -> Dict[Hashable, List[Tuple[int, float, float]]]:
        """Remaining spare capacity per credential after reservations.

        Returned intervals are ``(window_index, start, end)`` fragments.
        """
        used: Dict[Hashable, Dict[int, List[Tuple[float, float]]]] = {}
        credentials = {t.credential for t in self.tasks}
        for credential in credentials:
            used[credential] = {i: [] for i in range(len(self.windows))}
        for reservation in self.reservations:
            used[reservation.credential][reservation.window_index].append(
                (reservation.start, reservation.end)
            )

        spare: Dict[Hashable, List[Tuple[int, float, float]]] = {}
        for credential, per_window in used.items():
            fragments: List[Tuple[int, float, float]] = []
            for index, window in enumerate(self.windows):
                cursor = max(self.horizon_start, window.start)
                for busy_lo, busy_hi in sorted(per_window[index]):
                    if busy_lo > cursor + EPS:
                        fragments.append((index, cursor, busy_lo))
                    cursor = max(cursor, busy_hi)
                if window.end > cursor + EPS:
                    fragments.append((index, cursor, window.end))
            spare[credential] = fragments
        return spare

    def validate(self) -> bool:
        """Re-check every guarantee. Raises :class:`PlannerError` on violation."""
        problems: List[str] = []
        scheduled: Set[Hashable] = set()

        for reservation in self.reservations:
            if reservation.task_id in scheduled:
                problems.append(
                    "task %r is scheduled more than once" % (reservation.task_id,)
                )
            scheduled.add(reservation.task_id)
            try:
                task = self.task_by_id(reservation.task_id)
            except KeyError:
                problems.append(
                    "reservation references unknown task %r"
                    % (reservation.task_id,)
                )
                continue

            if not (0 <= reservation.window_index < len(self.windows)):
                problems.append(
                    "task %r uses an invalid window index %r"
                    % (task.task_id, reservation.window_index)
                )
                continue
            window = self.windows[reservation.window_index]

            # Non-preemptible: the whole reservation stays inside one window.
            if (
                reservation.start < max(self.horizon_start, window.start) - EPS
                or reservation.end > window.end + EPS
            ):
                problems.append(
                    "task %r reservation %s is not contained in window %s"
                    % (task.task_id, _fmt(reservation), _fmt(window))
                )

            if reservation.end > task.deadline + EPS:
                problems.append(
                    "task %r finishes at %s after its deadline %s"
                    % (task.task_id, reservation.end, task.deadline)
                )

            length = reservation.duration
            if length < task.duration_min - EPS or length > task.duration_max + EPS:
                problems.append(
                    "task %r reserves %s, outside duration bounds [%s, %s]"
                    % (task.task_id, length, task.duration_min, task.duration_max)
                )

            if task.mandatory and not (
                reservation.guaranteed and length >= task.duration_max - EPS
            ):
                problems.append(
                    "mandatory task %r is not reserved for its worst-case duration"
                    % (task.task_id,)
                )
            if reservation.guaranteed and length < task.duration_max - EPS:
                problems.append(
                    "task %r flagged guaranteed but reserves less than duration_max"
                    % (task.task_id,)
                )

        for task in self.mandatory_tasks:
            if task.task_id not in scheduled:
                problems.append(
                    "mandatory task %r is missing from the plan" % (task.task_id,)
                )

        # Credential exclusivity: no two reservations of one credential overlap.
        by_credential: Dict[Hashable, List[Reservation]] = {}
        for reservation in self.reservations:
            by_credential.setdefault(reservation.credential, []).append(reservation)
        for credential, group in by_credential.items():
            ordered = sorted(group, key=lambda r: r.start)
            for earlier, later in zip(ordered, ordered[1:]):
                if later.start < earlier.end - EPS:
                    problems.append(
                        "credential %r is double-booked by tasks %r and %r"
                        % (credential, earlier.task_id, later.task_id)
                    )
            # A best-effort slot must have no guaranteed booking after it in
            # the same window, so an overrun cannot reach a promised task.
            # Other best-effort tasks may follow: cascading optional failures
            # do not break any commitment.
            for reservation in group:
                if reservation.guaranteed:
                    continue
                blocked_by = [
                    other.task_id
                    for other in group
                    if other is not reservation
                    and other.window_index == reservation.window_index
                    and other.guaranteed
                    and other.start >= reservation.start - EPS
                ]
                if blocked_by:
                    problems.append(
                        "best-effort task %r is not at the tail of its window; "
                        "guaranteed bookings %r could be delayed by an overrun"
                        % (reservation.task_id, blocked_by)
                    )

        if problems:
            raise PlannerError("invalid plan: " + "; ".join(problems))
        return True

    def describe(self) -> str:
        lines = [
            "Guaranteed plan (horizon starts at %s)" % _fmt(self.horizon_start),
            "Availability windows:",
        ]
        for index, window in enumerate(self.windows):
            lines.append(
                "  W%d: [%s, %s)" % (index, _fmt(window.start), _fmt(window.end))
            )

        mandatory_ids = {t.task_id for t in self.mandatory_tasks}
        lines.append("Reservations (chronological):")
        for reservation in self.timeline():
            task = self.task_by_id(reservation.task_id)
            kind = "mandatory" if task.task_id in mandatory_ids else "optional"
            flag = "guaranteed" if reservation.guaranteed else "best-effort"
            lines.append(
                "  %s -> %s | task=%r credential=%r %s %s value=%s reserve=%s"
                % (
                    _fmt(reservation.start),
                    _fmt(reservation.end),
                    task.task_id,
                    reservation.credential,
                    kind,
                    flag,
                    task.value,
                    _fmt(reservation.duration),
                )
            )

        unscheduled = [
            t.task_id
            for t in self.tasks
            if t.task_id not in {r.task_id for r in self.reservations}
        ]
        if unscheduled:
            lines.append("Unscheduled tasks: %r" % (unscheduled,))
        lines.append(
            "Total value=%s (guaranteed=%s, best-effort=%s)"
            % (
                _fmt(self.total_value),
                _fmt(self.guaranteed_value),
                _fmt(self.best_effort_task_ids),
            )
        )
        return "\n".join(lines)


def build_plan(
    tasks: Sequence[Task],
    windows: Sequence[Window],
    horizon_start: float = 0,
) -> Plan:
    """Build a plan that guarantees every mandatory task.

    The procedure is:

    1. Validate inputs and clip availability windows to ``horizon_start``.
    2. Reject (with :class:`InfeasibleScheduleError`) when a mandatory
       task cannot fit even in isolation, or when the mandatory tasks of
       any credential cannot be packed using their worst-case
       (``duration_max``) durations. Placement is solved per credential
       with deadline-ordered backtracking, so a returned placement is a
       constructive proof of feasibility.
    3. Fill the remaining capacity with optional tasks, highest value
       density first, each trying a guaranteed (``duration_max``) slot
       before falling back to a best-effort (``duration_min``) slot.
    """
    normalized_tasks = list(tasks)
    normalized_windows = _normalize_windows(windows, horizon_start)

    seen_ids: Set[Hashable] = set()
    for task in normalized_tasks:
        task.validate()
        if task.task_id in seen_ids:
            raise InvalidTaskError("duplicate task id %r" % (task.task_id,))
        seen_ids.add(task.task_id)
    for window in normalized_windows:
        window.validate()

    for task in normalized_tasks:
        if task.mandatory and not _has_any_fit(
            task, task.duration_max, normalized_windows, horizon_start
        ):
            raise InfeasibleScheduleError(
                "mandatory task %r cannot finish within any window before its "
                "deadline %s even using its full duration_max %s"
                % (task.task_id, task.deadline, task.duration_max)
            )

    # Free intervals per credential, per window. Credentials are filled in
    # lazily as tasks reference them.
    free: Dict[Hashable, List[List[Tuple[float, float]]]] = {}
    # Latest end of a guaranteed booking per (credential, window). A
    # best-effort task may only use fragments at or after this boundary.
    guaranteed_front: Dict[Tuple[Hashable, int], float] = {}
    reservations: List[Reservation] = []

    def _mark_guaranteed(reservation: Reservation) -> None:
        key = (reservation.credential, reservation.window_index)
        guaranteed_front[key] = max(guaranteed_front.get(key, float("-inf")),
                                    reservation.end)

    mandatory = [t for t in normalized_tasks if t.mandatory]
    mandatory_by_credential: Dict[Hashable, List[Task]] = {}
    for task in mandatory:
        mandatory_by_credential.setdefault(task.credential, []).append(task)

    # Mandatory tasks of different credentials are independent; solve each
    # credential separately against its worst-case durations.
    for credential in sorted(mandatory_by_credential, key=lambda c: str(c)):
        group = mandatory_by_credential[credential]
        placements = _place_mandatory_group(
            credential, group, normalized_windows, horizon_start, free
        )
        for placement in placements:
            _mark_guaranteed(placement)
            reservations.append(placement)

    # Optional phase, run in two passes so a best-effort slot can never end
    # up in front of a guaranteed one.
    optional = [t for t in normalized_tasks if not t.mandatory]
    optional.sort(
        key=lambda t: (
            -(t.value / t.duration_min),
            -t.value,
            t.duration_min,
            str(t.task_id),
        )
    )

    # Pass 1: reserve duration_max where it fits -> fully guaranteed slots.
    unplaced: List[Task] = []
    for task in optional:
        reservation = _try_place(
            task, task.duration_max, True, normalized_windows, horizon_start, free
        )
        if reservation is None:
            unplaced.append(task)
        else:
            _mark_guaranteed(reservation)
            reservations.append(reservation)

    # Pass 2: best-effort slots from fragments beyond each credential's
    # guaranteed front. No guaranteed bookings are made afterwards, so an
    # overrun can only hit other best-effort tasks or idle time, never a
    # promised reservation.
    for task in unplaced:
        reservation = _try_place(
            task,
            task.duration_min,
            False,
            normalized_windows,
            horizon_start,
            free,
            tail_only=True,
            guaranteed_front=guaranteed_front,
        )
        if reservation is not None:
            reservations.append(reservation)

    plan = Plan(
        tasks=normalized_tasks,
        windows=normalized_windows,
        horizon_start=horizon_start,
        reservations=reservations,
    )
    plan.validate()
    return plan


def _normalize_windows(
    windows: Sequence[Window], horizon_start: float
) -> List[Window]:
    normalized: List[Window] = []
    for window in windows:
        window.validate()
        start = max(horizon_start, window.start)
        if window.end > start + EPS:
            normalized.append(Window(start=start, end=window.end))
    normalized.sort(key=lambda w: (w.start, w.end))
    return normalized


def _credential_view(
    free: Dict[Hashable, List[List[Tuple[float, float]]]],
    credential: Hashable,
    window_count: int,
    horizon_start: float,
    windows: Sequence[Window],
) -> List[List[Tuple[float, float]]]:
    if credential not in free:
        free[credential] = [
            [(max(horizon_start, window.start), window.end)] for window in windows
        ]
    return free[credential]


def _has_any_fit(
    task: Task,
    duration: float,
    windows: Sequence[Window],
    horizon_start: float,
) -> bool:
    for window in windows:
        lo = max(horizon_start, window.start)
        hi = min(window.end, task.deadline)
        if hi - lo >= duration - EPS:
            return True
    return False


def _earliest_slot(
    intervals: Sequence[Tuple[float, float]],
    duration: float,
    deadline: float,
) -> Optional[Tuple[float, int]]:
    """Return ``(start, interval_index)`` of the earliest fitting slot."""
    for index, (lo, hi) in enumerate(intervals):
        end = lo + duration
        if end <= hi + EPS and end <= deadline + EPS:
            return lo, index
    return None


def _consume(
    intervals: List[Tuple[float, float]],
    index: int,
    start: float,
    end: float,
) -> None:
    lo, hi = intervals.pop(index)
    if start > lo + EPS:
        intervals.insert(index, (lo, start))
        index += 1
    if hi > end + EPS:
        intervals.insert(index, (end, hi))


def _restore(
    intervals: List[Tuple[float, float]],
    index: int,
    start: float,
    end: float,
) -> None:
    """Undo a :func:`_consume` of ``[start, end)`` at ``index``."""
    intervals.insert(index, (start, end))
    merged: List[Tuple[float, float]] = []
    for lo, hi in sorted(intervals):
        if merged and lo <= merged[-1][1] + EPS:
            previous_lo, previous_hi = merged[-1]
            merged[-1] = (previous_lo, max(previous_hi, hi))
        else:
            merged.append((lo, hi))
    intervals[:] = merged


def _prefix_capacity_holds(
    remaining: Sequence[Task],
    intervals_per_window: Sequence[Sequence[Tuple[float, float]]],
) -> bool:
    """Order-independent pruning: per-deadline capacity must cover demand."""
    deadlines = sorted({task.deadline for task in remaining})
    for deadline in deadlines:
        demand = sum(
            task.duration_max
            for task in remaining
            if task.deadline <= deadline + EPS
        )
        capacity = 0.0
        for intervals in intervals_per_window:
            for lo, hi in intervals:
                capped_hi = min(hi, deadline)
                if capped_hi > lo + EPS:
                    capacity += capped_hi - lo
        if demand > capacity + EPS:
            return False
    return True


def _place_mandatory_group(
    credential: Hashable,
    group: Sequence[Task],
    windows: Sequence[Window],
    horizon_start: float,
    free: Dict[Hashable, List[List[Tuple[float, float]]]],
) -> List[Reservation]:
    intervals_per_window = _credential_view(
        free, credential, len(windows), horizon_start, windows
    )
    ordered = sorted(
        group, key=lambda t: (t.deadline, -t.duration_max, str(t.task_id))
    )
    budget = [SEARCH_NODE_BUDGET]
    placements: List[Tuple[Task, int, float, float]] = []

    if _backtrack(ordered, intervals_per_window, placements, budget):
        return [
            Reservation(
                task_id=task.task_id,
                credential=credential,
                window_index=window_index,
                start=start,
                end=end,
                guaranteed=True,
            )
            for task, window_index, start, end in placements
        ]

    # Search budget exhausted: fall back to one earliest-deadline greedy pass,
    # which is sound when it succeeds and clearly reports failure otherwise.
    greedy = _greedy_mandatory(credential, ordered, windows, horizon_start)
    if greedy is None:
        raise InfeasibleScheduleError(
            "mandatory tasks using credential %r cannot be guaranteed: no "
            "worst-case packing exists inside the given windows" % (credential,)
        )
    for reservation in greedy:
        _apply_reservation(reservation, free, windows, horizon_start)
    return greedy


def _backtrack(
    pending: Sequence[Task],
    intervals_per_window: List[List[Tuple[float, float]]],
    placements: List[Tuple[Task, int, float, float]],
    budget: List[int],
) -> bool:
    if not pending:
        return True
    budget[0] -= 1
    if budget[0] < 0:
        return False

    if not _prefix_capacity_holds(pending, intervals_per_window):
        return False

    # Candidates are tried in deadline order; permutations are explored
    # completely, so feasibility does not depend on one fixed ordering.
    for candidate_index, task in enumerate(pending):
        duration = task.duration_max
        for window_index, intervals in enumerate(intervals_per_window):
            fit = _earliest_slot(intervals, duration, task.deadline)
            if fit is None:
                continue
            start, interval_index = fit
            end = start + duration
            _consume(intervals, interval_index, start, end)
            placements.append((task, window_index, start, end))
            rest = pending[:candidate_index] + pending[candidate_index + 1 :]
            if _backtrack(rest, intervals_per_window, placements, budget):
                return True
            placements.pop()
            _restore(intervals, interval_index, start, end)
    return False


def _greedy_mandatory(
    credential: Hashable,
    group: Sequence[Task],
    windows: Sequence[Window],
    horizon_start: float,
) -> Optional[List[Reservation]]:
    intervals_per_window: List[List[Tuple[float, float]]] = [
        [(max(horizon_start, window.start), window.end)] for window in windows
    ]
    reservations: List[Reservation] = []
    for task in group:
        placed = False
        for window_index, intervals in enumerate(intervals_per_window):
            fit = _earliest_slot(intervals, task.duration_max, task.deadline)
            if fit is None:
                continue
            start, interval_index = fit
            end = start + task.duration_max
            _consume(intervals, interval_index, start, end)
            reservations.append(
                Reservation(
                    task_id=task.task_id,
                    credential=credential,
                    window_index=window_index,
                    start=start,
                    end=end,
                    guaranteed=True,
                )
            )
            placed = True
            break
        if not placed:
            return None
    return reservations


def _try_place(
    task: Task,
    duration: float,
    guaranteed: bool,
    windows: Sequence[Window],
    horizon_start: float,
    free: Dict[Hashable, List[List[Tuple[float, float]]]],
    tail_only: bool = False,
    guaranteed_front: Optional[Dict[Tuple[Hashable, int], float]] = None,
) -> Optional[Reservation]:
    intervals_per_window = _credential_view(
        free, task.credential, len(windows), horizon_start, windows
    )
    for window_index, intervals in enumerate(intervals_per_window):
        if tail_only:
            # Only fragments at or after this credential's guaranteed front
            # are eligible, so an overrun cannot reach a guaranteed booking.
            front = (guaranteed_front or {}).get(
                (task.credential, window_index), float("-inf")
            )
            eligible = [
                (index, fragment)
                for index, fragment in enumerate(intervals)
                if fragment[0] >= front - EPS
            ]
            fit = None
            for index, (lo, hi) in eligible:
                end = lo + duration
                if end <= hi + EPS and end <= task.deadline + EPS:
                    fit = (lo, index)
                    break
            if fit is None:
                continue
            start, interval_index = fit
            end = start + duration
        else:
            fit = _earliest_slot(intervals, duration, task.deadline)
            if fit is None:
                continue
            start, interval_index = fit
            end = start + duration
        reservation = Reservation(
            task_id=task.task_id,
            credential=task.credential,
            window_index=window_index,
            start=start,
            end=end,
            guaranteed=guaranteed,
        )
        _consume(intervals, interval_index, start, end)
        return reservation
    return None


def _apply_reservation(
    reservation: Reservation,
    free: Dict[Hashable, List[List[Tuple[float, float]]]],
    windows: Sequence[Window],
    horizon_start: float,
) -> None:
    intervals_per_window = _credential_view(
        free, reservation.credential, len(windows), horizon_start, windows
    )
    intervals = intervals_per_window[reservation.window_index]
    for index, (lo, hi) in enumerate(intervals):
        if lo <= reservation.start + EPS and reservation.end <= hi + EPS:
            _consume(intervals, index, reservation.start, reservation.end)
            return
    raise PlannerError(
        "internal error: cannot book reservation for task %r"
        % (reservation.task_id,)
    )


def _fmt(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
