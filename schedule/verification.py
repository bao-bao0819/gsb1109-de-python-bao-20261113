"""
Exact (non-probabilistic) analysis of a publishing commitment built on top
of two staggered ``every(a).to(b)`` collection jobs.

Two collection sources are each registered as a randomized job
(``scheduler.every(a).to(b).minutes``) on one shared serial worker. Once both
sources have delivered all required collections, a fixed reconciliation
buffer must still fit inside the limited business window.

The randomized interval drawn inside :meth:`Job._schedule_next_run` is
``random.randint(a, b)``, so every interval of a source is an independent
integer in the closed range ``[a, b]``. This module never samples runs to
argue about liveness: it derives exact best/worst-case completion bounds and
adds :func:`replay_commitment`, a deterministic replayer that drives the real
:class:`~schedule.Scheduler` with an explicit, replayable sequence of drawn
intervals and a virtual clock.
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, List, Mapping, Sequence
from unittest import mock

import schedule

_VALID_UNITS = ("seconds", "minutes", "hours")


class Verdict(str, Enum):
    """Three-way classification of the publishing commitment."""

    GUARANTEED = "guaranteed"
    POSSIBLE = "possible"
    IMPOSSIBLE = "impossible"


@dataclass(frozen=True)
class SourceSpec:
    """
    A randomized collection source.

    :param name: Unique source identifier, also used as the replay key.
    :param min_interval: Lower bound ``a`` of ``every(a).to(b)``.
    :param max_interval: Upper bound ``b`` of ``every(a).to(b)``.
    :param required_runs: Number of collections that must complete.
    :param unit: Time unit of the intervals (seconds/minutes/hours).
    """

    name: str
    min_interval: int
    max_interval: int
    required_runs: int = 1
    unit: str = "minutes"

    def __post_init__(self) -> None:
        if self.unit not in _VALID_UNITS:
            raise ValueError(
                "unit must be one of %s, got %r" % (_VALID_UNITS, self.unit)
            )
        if self.min_interval < 1:
            raise ValueError("min_interval must be >= 1")
        if self.max_interval < self.min_interval:
            raise ValueError("max_interval must be >= min_interval")
        if self.required_runs < 1:
            raise ValueError("required_runs must be >= 1")

    @property
    def fastest_completion(self) -> timedelta:
        """Elapsed time to completion if every draw equals ``min_interval``."""
        return timedelta(
            **{self.unit: self.min_interval * self.required_runs}
        )

    @property
    def slowest_completion(self) -> timedelta:
        """Elapsed time to completion if every draw equals ``max_interval``."""
        return timedelta(
            **{self.unit: self.max_interval * self.required_runs}
        )


@dataclass(frozen=True)
class CommitmentAnalysis:
    """Exact bound analysis for the two-source commitment."""

    open_at: datetime
    close_at: datetime
    horizon: timedelta
    reconciliation_buffer: timedelta
    earliest_completion: timedelta
    latest_completion: timedelta
    verdict: Verdict

    @property
    def earliest_publish_ready(self) -> timedelta:
        return self.earliest_completion + self.reconciliation_buffer

    @property
    def latest_publish_ready(self) -> timedelta:
        return self.latest_completion + self.reconciliation_buffer


@dataclass(frozen=True)
class RunEvent:
    """A single recorded job execution during a replay."""

    source: str
    run_index: int
    scheduled_for: datetime
    executed_at: datetime
    drawn_interval: int


@dataclass(frozen=True)
class ReplayReport:
    """Result of one deterministic replay of explicit interval draws."""

    success: bool
    open_at: datetime
    close_at: datetime
    reconciliation_buffer: timedelta
    completion_at: datetime
    publish_ready_at: datetime
    margin: timedelta
    tie_order_respected: bool
    draws_used: Mapping[str, List[int]]
    events: List[RunEvent] = field(default_factory=list)


def analyze_commitment(
    open_at: datetime,
    close_at: datetime,
    reconciliation_minutes: float,
    sources: Sequence[SourceSpec],
) -> CommitmentAnalysis:
    """
    Classify the commitment without running anything randomized.

    Because jobs on the shared serial worker are modeled as zero-duration
    (the scheduler itself does not account for job duration), collection is
    complete at the later of the two sources' completion instants. Each
    source's completion is a sum of ``required_runs`` independent draws from
    its closed ``[min_interval, max_interval]`` range, so its exact bounds
    are ``runs * min_interval`` and ``runs * max_interval``.

    :param open_at: Start of the business window / registration moment.
    :param close_at: End of the business window (publishing deadline).
    :param reconciliation_minutes: Fixed buffer reserved after both sources
        have completed their collections.
    :param sources: The two (or more) collection source specifications.
    """
    horizon, buffer_delta = _validate_window(
        open_at, close_at, reconciliation_minutes
    )
    _validate_sources(sources)

    earliest_completion = max(s.fastest_completion for s in sources)
    latest_completion = max(s.slowest_completion for s in sources)

    if latest_completion + buffer_delta <= horizon:
        verdict = Verdict.GUARANTEED
    elif earliest_completion + buffer_delta > horizon:
        verdict = Verdict.IMPOSSIBLE
    else:
        verdict = Verdict.POSSIBLE

    return CommitmentAnalysis(
        open_at=open_at,
        close_at=close_at,
        horizon=horizon,
        reconciliation_buffer=buffer_delta,
        earliest_completion=earliest_completion,
        latest_completion=latest_completion,
        verdict=verdict,
    )


def minimum_draws(sources: Sequence[SourceSpec]) -> Dict[str, List[int]]:
    """Fastest corner: every source always draws its minimum interval."""
    return {
        s.name: [s.min_interval] * s.required_runs for s in sources
    }


def maximum_draws(sources: Sequence[SourceSpec]) -> Dict[str, List[int]]:
    """Slowest corner: every source always draws its maximum interval."""
    return {
        s.name: [s.max_interval] * s.required_runs for s in sources
    }


def replay_commitment(
    open_at: datetime,
    close_at: datetime,
    reconciliation_minutes: float,
    sources: Sequence[SourceSpec],
    draws: Mapping[str, Sequence[int]],
) -> ReplayReport:
    """
    Replay the commitment deterministically against the real scheduler.

    The virtual clock jumps straight to each scheduled tick, the genuine
    :meth:`Scheduler.run_pending` is invoked, and ``random.randint`` calls
    made by :meth:`Job._schedule_next_run` are served per source from the
    explicit ``draws`` sequences. No randomness is left to chance: the same
    ``draws`` always produce the same report.

    Jobs return :class:`~schedule.CancelJob` after their final required run,
    so exactly ``required_runs`` intervals are drawn per source and every
    supplied value must be consumed. When two jobs are due at the same tick,
    the scheduler's stable sort executes them in registration order; the
    replay asserts this ordering is observed.

    :param draws: Explicit interval sequence per source name. Each sequence
        must contain exactly ``required_runs`` integers inside the source's
        ``[min_interval, max_interval]`` range.
    """
    _horizon, buffer_delta = _validate_window(
        open_at, close_at, reconciliation_minutes
    )
    _validate_sources(sources)
    _validate_draws(sources, draws)

    scheduler = schedule.Scheduler()
    clock = {"now": open_at}
    events: List[RunEvent] = []
    run_counts: Dict[str, int] = {s.name: 0 for s in sources}
    draws_used: Dict[str, List[int]] = {s.name: [] for s in sources}
    scripts: Dict[str, deque] = {
        s.name: deque(draws[s.name]) for s in sources
    }
    jobs_by_name: Dict[str, schedule.Job] = {}
    registration_order = [s.name for s in sources]
    current: Dict[str, str] = {}

    class MockDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = clock["now"]
            return value.astimezone(tz) if tz is not None else value

    def fake_randint(a: int, b: int) -> int:
        spec = next(s for s in sources if s.name == current["spec"])
        if a != spec.min_interval or b != spec.max_interval:
            raise AssertionError(
                "randint(%s, %s) does not match every(%s).to(%s) of source %r"
                % (a, b, spec.min_interval, spec.max_interval, spec.name)
            )
        if run_counts[spec.name] >= spec.required_runs:
            # Job.run() always reschedules before CancelJob is handled, but
            # the final collection already happened: this extra reschedule
            # is discarded and must not consume a supplied draw.
            return spec.min_interval
        value = scripts[spec.name].popleft()
        draws_used[spec.name].append(value)
        return value

    def make_collector(spec: SourceSpec):
        def collector():
            current["spec"] = spec.name
            target_job = jobs_by_name[spec.name]
            run_counts[spec.name] += 1
            run_index = run_counts[spec.name]
            events.append(
                RunEvent(
                    source=spec.name,
                    run_index=run_index,
                    scheduled_for=target_job.next_run,
                    executed_at=clock["now"],
                    drawn_interval=draws_used[spec.name][run_index - 1],
                )
            )
            if run_index == spec.required_runs:
                return schedule.CancelJob
            return None

        collector.__name__ = "collect_%s" % spec.name
        return collector

    # The clock must be patched before job.do(): registration itself invokes
    # _schedule_next_run() and anchors the first run to "now".
    with mock.patch.object(
        schedule.datetime, "datetime", MockDateTime
    ), mock.patch.object(schedule.random, "randint", fake_randint):
        for spec in sources:
            job = scheduler.every(spec.min_interval).to(spec.max_interval)
            getattr(job, spec.unit)
            jobs_by_name[spec.name] = job
            current["spec"] = spec.name
            job.do(make_collector(spec))

        while scheduler.jobs:
            next_runs = [job.next_run for job in scheduler.jobs]
            clock["now"] = min(run for run in next_runs if run is not None)
            scheduler.run_pending()

    for spec in sources:
        if run_counts[spec.name] != spec.required_runs:
            raise AssertionError(
                "source %r completed %s runs, expected %s"
                % (spec.name, run_counts[spec.name], spec.required_runs)
            )

    tie_order_respected = _check_tie_order(events, registration_order)

    completion_at = max(event.executed_at for event in events)
    publish_ready_at = completion_at + buffer_delta
    success = publish_ready_at <= close_at
    margin = close_at - publish_ready_at

    return ReplayReport(
        success=success,
        open_at=open_at,
        close_at=close_at,
        reconciliation_buffer=buffer_delta,
        completion_at=completion_at,
        publish_ready_at=publish_ready_at,
        margin=margin,
        tie_order_respected=tie_order_respected,
        draws_used=draws_used,
        events=events,
    )


def _validate_window(
    open_at: datetime, close_at: datetime, reconciliation_minutes: float
):
    if not isinstance(open_at, datetime) or not isinstance(close_at, datetime):
        raise TypeError("open_at and close_at must be datetime instances")
    horizon = close_at - open_at
    if horizon <= timedelta(0):
        raise ValueError("close_at must be later than open_at")
    if reconciliation_minutes < 0:
        raise ValueError("reconciliation_minutes must be >= 0")
    buffer_delta = timedelta(minutes=reconciliation_minutes)
    return horizon, buffer_delta


def _validate_sources(sources: Sequence[SourceSpec]) -> None:
    if len(sources) < 2:
        raise ValueError("at least two collection sources are required")
    names = [s.name for s in sources]
    if len(set(names)) != len(names):
        raise ValueError("source names must be unique: %r" % names)


def _validate_draws(
    sources: Sequence[SourceSpec], draws: Mapping[str, Sequence[int]]
) -> None:
    for spec in sources:
        if spec.name not in draws:
            raise KeyError("missing replay draws for source %r" % spec.name)
        sequence = draws[spec.name]
        if len(sequence) != spec.required_runs:
            raise ValueError(
                "source %r needs %s drawn intervals, got %s"
                % (spec.name, spec.required_runs, len(sequence))
            )
        for value in sequence:
            if not isinstance(value, int):
                raise TypeError(
                    "drawn intervals for source %r must be int" % spec.name
                )
            if not spec.min_interval <= value <= spec.max_interval:
                raise ValueError(
                    "drawn interval %s for source %r is outside [%s, %s]"
                    % (value, spec.name, spec.min_interval, spec.max_interval)
                )
    extra = set(draws) - {s.name for s in sources}
    if extra:
        raise ValueError("draws supplied for unknown sources: %r" % sorted(extra))


def _check_tie_order(
    events: Sequence[RunEvent], registration_order: Sequence[str]
) -> bool:
    priority = {name: index for index, name in enumerate(registration_order)}
    for earlier, later in zip(events, events[1:]):
        if earlier.executed_at == later.executed_at:
            if priority[earlier.source] > priority[later.source]:
                return False
    return True
