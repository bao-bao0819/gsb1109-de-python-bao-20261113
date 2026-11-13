"""
Reconstruct workstation usage from sparse production logs.

The production log only records, for each job (order), its process start/end
timestamps and a workstation *type*. A handful of inspection records pin a
specific job to one concrete workstation. Nothing else says which physical
workstation served which job.

Rules supplied by the shop floor:

* Once a job starts it cannot move workstations.
* One workstation cannot run two jobs at the same time.
* A job ending exactly when the next one starts is a legal hand-off, so
  intervals are treated as half-open ``[start, end)`` — the same convention
  :class:`intervaltree.IntervalTree` uses natively, which makes endpoint
  touching automatically legal.

The :class:`Reconstruction` engine keeps one interval tree per workstation and
uses backtracking with a minimum-remaining-values heuristic to find full-shift
placements, then classifies every job and every unplaceable time window.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from intervaltree import IntervalTree


@dataclass(frozen=True)
class JobLog:
    """One process record from the production log."""

    job_id: str
    start: int
    end: int
    station_type: str
    pinned_station: Optional[str] = None

    def __post_init__(self):
        if self.end <= self.start:
            raise ValueError(
                "job %r must have end > start, got [%r, %r)"
                % (self.job_id, self.start, self.end)
            )

    @property
    def span(self) -> Tuple[int, int]:
        return self.start, self.end


@dataclass
class Reconstruction:
    """
    Reconstruct workstation assignments for one shift.

    Parameters
    ----------
    jobs:
        Parsed production-log records, in any order.
    stations_by_type:
        Maps every workstation *type* to the concrete workstation ids of that
        type.
    solution_limit:
        Safety cap on full schedules enumerated during classification.
    """

    jobs: Sequence[JobLog]
    stations_by_type: Mapping[str, Sequence[str]]
    solution_limit: int = 10000

    jobs_by_id: Dict[str, JobLog] = field(init=False)
    station_types: Dict[str, str] = field(init=False)
    _candidates: Dict[str, Tuple[str, ...]] = field(init=False)

    def __post_init__(self):
        self.jobs_by_id = {job.job_id: job for job in self.jobs}
        if len(self.jobs_by_id) != len(self.jobs):
            raise ValueError("duplicate job ids are not allowed")

        self.station_types = {}
        for station_type, stations in self.stations_by_type.items():
            if not stations:
                raise ValueError("station type %r has no workstations" % station_type)
            for station in stations:
                if station in self.station_types:
                    raise ValueError("station %r listed under two types" % station)
                self.station_types[station] = station_type

        self._candidates = {
            job.job_id: self._initial_candidates(job) for job in self.jobs
        }
        self._validate_pins()

    # ------------------------------------------------------------------ setup
    def _initial_candidates(self, job: JobLog) -> Tuple[str, ...]:
        if job.station_type not in self.stations_by_type:
            raise ValueError(
                "job %r needs unknown station type %r"
                % (job.job_id, job.station_type)
            )
        stations = tuple(self.stations_by_type[job.station_type])
        if job.pinned_station is not None:
            if job.pinned_station not in stations:
                raise ValueError(
                    "inspection pin for job %r points at %r, which is not a %s station"
                    % (job.job_id, job.pinned_station, job.station_type)
                )
            return (job.pinned_station,)
        return stations

    def _validate_pins(self):
        """Two inspection-pinned jobs overlapping must not share a station."""
        pinned = [job for job in self.jobs if job.pinned_station is not None]
        for index, job in enumerate(pinned):
            for other in pinned[index + 1 :]:
                if (
                    job.pinned_station == other.pinned_station
                    and job.start < other.end
                    and other.start < job.end
                ):
                    raise ValueError(
                        "inspection records force jobs %r and %r onto the same "
                        "station %r over an overlapping interval"
                        % (job.job_id, other.job_id, job.pinned_station)
                    )

    # ------------------------------------------------------------- feasibility
    @staticmethod
    def _fits(tree: IntervalTree, start: int, end: int) -> bool:
        """Half-open overlap test: empty result means a legal placement."""
        return not tree.overlap(start, end)

    def _legal_stations(
        self, job_id: str, trees: Mapping[str, IntervalTree]
    ) -> List[str]:
        job = self.jobs_by_id[job_id]
        return [
            station
            for station in self._candidates[job_id]
            if self._fits(trees[station], job.start, job.end)
        ]

    def _solve(
        self,
        trees: Mapping[str, IntervalTree],
        remaining: List[str],
        assignments: Dict[str, str],
        sink: Optional[List[Dict[str, str]]] = None,
        limit: Optional[int] = None,
    ) -> Optional[Dict[str, str]]:
        """
        Backtracking search with the minimum-remaining-values heuristic.

        With ``sink`` given, every complete schedule is collected (up to
        ``limit``) and a non-None sentinel is returned once the cap is hit;
        otherwise the first complete schedule found is returned.
        """
        if not remaining:
            if sink is not None:
                sink.append(dict(assignments))
                if limit is not None and len(sink) >= limit:
                    return {}
                return None
            return dict(assignments)

        scored = []
        for position, job_id in enumerate(remaining):
            options = self._legal_stations(job_id, trees)
            if not options:
                return None
            scored.append((len(options), position, job_id, options))
        scored.sort(
            key=lambda item: (
                item[0],
                self.jobs_by_id[item[2]].start,
                item[2],
            )
        )
        _, position, job_id, options = scored[0]

        job = self.jobs_by_id[job_id]
        rest = remaining[:position] + remaining[position + 1 :]
        for station in options:
            trees[station].addi(job.start, job.end, job_id)
            assignments[job_id] = station
            result = self._solve(trees, rest, assignments, sink, limit)
            trees[station].removei(job.start, job.end, job_id)
            del assignments[job_id]
            if sink is None:
                if result is not None:
                    return result
            elif result is not None:
                return result
        return None

    def _fresh_trees(self) -> Dict[str, IntervalTree]:
        return {station: IntervalTree() for station in self.station_types}

    def first_solution(self) -> Optional[Dict[str, str]]:
        return self._solve(self._fresh_trees(), list(self.jobs_by_id), {})

    def all_solutions(self, limit: int) -> Tuple[List[Dict[str, str]], bool]:
        sink: List[Dict[str, str]] = []
        capped = self._solve(
            self._fresh_trees(), list(self.jobs_by_id), {}, sink, limit
        )
        return sink, capped is not None

    # ------------------------------------------------------------ classification
    def classify(self) -> Dict[str, object]:
        """
        Classify every job and every unplaceable time window.

        Jobs fall into three groups:

        * ``unique``      — feasible, and every full-shift placement agrees on
          the workstation;
        * ``ambiguous``   — feasible on at least two different workstations,
          each extendable to a schedule satisfying the whole log;
        * ``unschedulable`` — no full-shift placement exists; the report also
          carries the concrete overloaded time windows that cannot fit.
        """
        solutions, truncated = self.all_solutions(self.solution_limit)

        if not solutions:
            overload = self._overload_windows()
            pin_witness = self._pin_witness()
            affected = set(overload["affected_jobs"])
            if not affected and pin_witness:
                affected.update(pin_witness["violated_pins"])
            return {
                "status": "infeasible",
                "truncated": truncated,
                "unique": {},
                "ambiguous": {},
                "unschedulable_jobs": sorted(affected),
                "overload_windows": overload["windows"],
                "pin_witness": pin_witness,
                "idle_windows": {},
                "solutions": [],
            }

        stations_used: Dict[str, set] = defaultdict(set)
        for schedule in solutions:
            for job_id, station in schedule.items():
                stations_used[job_id].add(station)

        unique: Dict[str, str] = {}
        ambiguous: Dict[str, List[str]] = {}
        for job_id in sorted(self.jobs_by_id):
            stations = sorted(stations_used[job_id])
            if len(stations) == 1:
                unique[job_id] = stations[0]
            else:
                ambiguous[job_id] = stations

        reference = solutions[0]
        return {
            "status": "feasible",
            "truncated": truncated,
            "unique": unique,
            "ambiguous": ambiguous,
            "unschedulable_jobs": [],
            "overload_windows": {},
            "pin_witness": None,
            "idle_windows": self._idle_windows(reference),
            "stations_used": {
                job_id: sorted(stations) for job_id, stations in stations_used.items()
            },
            "solutions": solutions,
        }

    # ------------------------------------------------------- impossible windows
    def _shift_bounds(self) -> Tuple[int, int]:
        return (
            min(job.start for job in self.jobs),
            max(job.end for job in self.jobs),
        )

    def _overload_windows(self) -> Dict[str, object]:
        """
        Sweep each workstation type for demand that exceeds its station count.

        At any instant, jobs of one type needing more parallel workstations
        than the type owns can never be placed; those elementary segments are
        merged into overload windows, which are the "time that simply cannot
        be scheduled". Jobs active during such a window are reported too.
        """
        shift_start, shift_end = self._shift_bounds()
        windows: Dict[str, List[Dict[str, object]]] = {}
        affected: set = set()

        for station_type, stations in self.stations_by_type.items():
            type_jobs = [job for job in self.jobs if job.station_type == station_type]
            boundaries = {shift_start, shift_end}
            for job in type_jobs:
                boundaries.add(job.start)
                boundaries.add(job.end)
            ticks = sorted(boundaries)

            overload_tree = IntervalTree()
            for left, right in zip(ticks, ticks[1:]):
                midpoint = (left + right) / 2
                active = [
                    job for job in type_jobs if job.start <= midpoint < job.end
                ]
                if len(active) > len(stations):
                    overload_tree.addi(
                        left,
                        right,
                        {
                            "demand": len(active),
                            "capacity": len(stations),
                            "jobs": sorted(job.job_id for job in active),
                        },
                    )
                    affected.update(job.job_id for job in active)

            def merge_data(left, right):
                return {
                    "demand": max(left["demand"], right["demand"]),
                    "capacity": left["capacity"],
                    "jobs": sorted(set(left["jobs"]) | set(right["jobs"])),
                }

            overload_tree.merge_overlaps(strict=False, data_reducer=merge_data)
            windows[station_type] = [
                {
                    "begin": interval.begin,
                    "end": interval.end,
                    "demand": interval.data["demand"],
                    "capacity": interval.data["capacity"],
                    "jobs": interval.data["jobs"],
                }
                for interval in sorted(overload_tree)
            ]

        return {
            "windows": {key: value for key, value in windows.items() if value},
            "affected_jobs": affected,
        }

    def _pin_witness(self) -> Optional[Dict[str, object]]:
        """
        Attribute an infeasibility with no instantaneous overload to the
        inspection pins.

        Re-solves after temporarily dropping every pin: if the relaxed log
        fits, the pins are the root cause, and one relaxed schedule plus the
        violated pins are returned as evidence.
        """
        unpinned = [
            JobLog(job.job_id, job.start, job.end, job.station_type, None)
            for job in self.jobs
        ]
        relaxed = Reconstruction(unpinned, self.stations_by_type, self.solution_limit)
        relaxed_schedule = relaxed.first_solution()
        if relaxed_schedule is None:
            return None

        pins = {
            job.job_id: job.pinned_station
            for job in self.jobs
            if job.pinned_station is not None
        }
        violated = {
            job_id: {
                "pinned_to": station,
                "relaxed_uses": relaxed_schedule[job_id],
            }
            for job_id, station in pins.items()
            if relaxed_schedule[job_id] != station
        }
        return {
            "relaxed_schedule": relaxed_schedule,
            "violated_pins": violated,
        }

    # ------------------------------------------------------------- idle windows
    def _idle_windows(
        self, reference: Mapping[str, str]
    ) -> Dict[str, List[Dict[str, int]]]:
        """
        Complement of each workstation's busy tree over the shift span.

        The special ``__any_station__`` entry lists the times when every
        workstation of every type is idle, i.e. plant-wide unused capacity.
        """
        shift_start, shift_end = self._shift_bounds()
        busy: Dict[str, IntervalTree] = defaultdict(IntervalTree)
        plant_busy = IntervalTree()
        for job_id, station in reference.items():
            job = self.jobs_by_id[job_id]
            busy[station].addi(job.start, job.end, job_id)
            plant_busy.addi(job.start, job.end, station)

        def gaps(tree: IntervalTree) -> List[Dict[str, int]]:
            covered = IntervalTree(tree)
            covered.merge_overlaps(strict=False, data_reducer=lambda a, b: a)
            result: List[Dict[str, int]] = []
            cursor = shift_start
            for interval in sorted(covered):
                if interval.begin > cursor:
                    result.append({"begin": cursor, "end": interval.begin})
                cursor = max(cursor, interval.end)
            if cursor < shift_end:
                result.append({"begin": cursor, "end": shift_end})
            return result

        windows = {station: gaps(busy[station]) for station in sorted(self.station_types)}
        windows["__any_station__"] = gaps(plant_busy)
        return windows

    # -------------------------------------------------------------- ambiguity
    def demonstrate_ambiguity(
        self, job_id: str
    ) -> Tuple[Dict[str, str], Dict[str, str]]:
        """
        Produce two full-shift schedules that satisfy every log record but put
        ``job_id`` on different workstations.

        Raises ``KeyError`` for unknown jobs and ``ValueError`` when the job
        is not genuinely ambiguous.
        """
        if job_id not in self.jobs_by_id:
            raise KeyError(job_id)

        pair: List[Dict[str, str]] = []
        seen_stations: set = set()
        trees = self._fresh_trees()
        self._collect_distinct(
            trees, list(self.jobs_by_id), {}, job_id, pair, seen_stations
        )

        if len(pair) < 2:
            raise ValueError(
                "job %r has no two distinct feasible placements" % job_id
            )
        return pair[0], pair[1]

    def _collect_distinct(
        self,
        trees: Mapping[str, IntervalTree],
        remaining: List[str],
        assignments: Dict[str, str],
        target: str,
        pair: List[Dict[str, str]],
        seen_stations: set,
    ) -> Optional[dict]:
        if not remaining:
            station = assignments[target]
            if station not in seen_stations:
                seen_stations.add(station)
                pair.append(dict(assignments))
                if len(pair) == 2:
                    return {}
            return None

        scored = []
        for position, current in enumerate(remaining):
            options = self._legal_stations(current, trees)
            if not options:
                return None
            scored.append((len(options), position, current, options))
        scored.sort(
            key=lambda item: (
                item[0],
                self.jobs_by_id[item[2]].start,
                item[2],
            )
        )
        _, position, current, options = scored[0]

        job = self.jobs_by_id[current]
        rest = remaining[:position] + remaining[position + 1 :]
        for station in sorted(options):
            trees[station].addi(job.start, job.end, current)
            assignments[current] = station
            result = self._collect_distinct(
                trees, rest, assignments, target, pair, seen_stations
            )
            trees[station].removei(job.start, job.end, current)
            del assignments[current]
            if result is not None:
                return result
        return None

    # ------------------------------------------------------------------ report
    def utilization(self, schedule: Mapping[str, str]) -> Dict[str, float]:
        """Busy fraction of each workstation over the full shift span."""
        shift_start, shift_end = self._shift_bounds()
        span = shift_end - shift_start
        busy: Dict[str, int] = defaultdict(int)
        for job_id, station in schedule.items():
            job = self.jobs_by_id[job_id]
            busy[station] += job.end - job.start
        return {
            station: round(busy.get(station, 0) / span, 6)
            for station in sorted(self.station_types)
        }


def verify(
    jobs: Sequence[JobLog],
    stations_by_type: Mapping[str, Sequence[str]],
    schedule: Mapping[str, str],
) -> List[str]:
    """
    Independently validate a full schedule against the raw log.

    Returns a list of violations; an empty list means the schedule is legal.
    """
    violations: List[str] = []
    trees: Dict[str, IntervalTree] = defaultdict(IntervalTree)
    known = {station for stations in stations_by_type.values() for station in stations}
    type_lookup = {
        station: station_type
        for station_type, stations in stations_by_type.items()
        for station in stations
    }

    if set(schedule) != {job.job_id for job in jobs}:
        violations.append(
            "schedule covers %r but log has %r"
            % (sorted(schedule), sorted(job.job_id for job in jobs))
        )

    for job in jobs:
        station = schedule.get(job.job_id)
        if station is None:
            continue
        if station not in known:
            violations.append(
                "job %r assigned unknown station %r" % (job.job_id, station)
            )
            continue
        if type_lookup[station] != job.station_type:
            violations.append(
                "job %r needs type %r but station %r is %r"
                % (job.job_id, job.station_type, station, type_lookup[station])
            )
        if job.pinned_station is not None and station != job.pinned_station:
            violations.append(
                "job %r pinned to %r by inspection but placed at %r"
                % (job.job_id, job.pinned_station, station)
            )
        trees[station].addi(job.start, job.end, job.job_id)

    for station, tree in trees.items():
        for interval in tree:
            clashes = [
                other
                for other in tree.overlap(interval.begin, interval.end)
                if interval.data < other.data
            ]
            for other in clashes:
                violations.append(
                    "station %r double-booked: jobs %r [%r,%r) and %r [%r,%r)"
                    % (
                        station,
                        interval.data,
                        interval.begin,
                        interval.end,
                        other.data,
                        other.begin,
                        other.end,
                    )
                )
    return violations
