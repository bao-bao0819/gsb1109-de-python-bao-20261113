#
#   Copyright (C) 2012 - 2024 Christian Ledermann
#
#   This library is free software; you can redistribute it and/or
#   modify it under the terms of the GNU Lesser General Public
#   License as published by the Free Software Foundation; either
#   version 2.1 of the License, or (at your option) any later version.

#   This library is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#   Lesser General Public License for more details.

#   You should have received a copy of the GNU Lesser General Public License
#   along with this library; if not, write to the Free Software Foundation,
#   Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301  USA
#
"""Topology preserving simplification of shared polygon boundaries."""

import math
from collections.abc import Iterable
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from itertools import pairwise
from typing import cast

from pygeoif.functions import _orientation
from pygeoif.functions import _point_segment_distance
from pygeoif.functions import is_simple_ring
from pygeoif.functions import simplify_line
from pygeoif.types import LineType
from pygeoif.types import PointType
from pygeoif.types import PolygonType


@dataclass(slots=True, eq=False)
class _Simplifiable:
    """A coordinate sequence that may lose interior points during simplification."""

    original: tuple[PointType, ...]
    kept: set[int] = field(default_factory=set)

    @property
    def points(self) -> list[PointType]:
        """Return the currently retained coordinates in their original order."""
        return [self.original[index] for index in sorted(self.kept)]

    @property
    def removed(self) -> list[int]:
        """Return the indices of the coordinates that are currently removed."""
        return [
            index
            for index in range(1, len(self.original) - 1)
            if index not in self.kept
        ]

    def simplify(
        self,
        tolerance: float,
        preserve: Iterable[PointType],
    ) -> None:
        """Run Douglas-Peucker and remember which indices survived."""
        simplified = simplify_line(
            cast("LineType", self.original),
            tolerance,
            preserve,
        )
        kept: set[int] = set()
        cursor = 0
        for point in simplified:
            while self.original[cursor] != point:
                cursor += 1
            kept.add(cursor)
            cursor += 1
        self.kept = kept

    def restore(self, index: int) -> None:
        """Put a previously removed coordinate back into the sequence."""
        self.kept.add(index)

    def removal_priority(self) -> tuple[float, int]:
        """Score the most valuable removed point by its distance to the line."""
        line = self.points
        best_index = -1
        best_distance = -1.0
        for index in self.removed:
            point = self.original[index]
            distance = min(
                _point_segment_distance(point, line[seg], line[seg + 1])
                for seg in range(len(line) - 1)
            )
            if distance > best_distance:
                best_distance = distance
                best_index = index
        return best_distance, best_index


@dataclass(slots=True)
class _RingEntry:
    """One stretch of a ring, either a shared edge or a polygon private stretch."""

    unit: _Simplifiable
    reversed: bool


def _collinear_overlap(
    first_start: PointType,
    first_end: PointType,
    second_start: PointType,
    second_end: PointType,
) -> bool:
    """
    Check whether two segments lie on the same supporting line and overlap.

    A mere touch at a common endpoint (zero length overlap) is not enough,
    otherwise polygon corners shared by several parcels would be mistaken
    for shared boundaries.
    """
    dx = first_end[0] - first_start[0]
    dy = first_end[1] - first_start[1]
    length_squared = dx * dx + dy * dy
    if length_squared == 0.0:
        return False
    if _orientation(first_start, first_end, second_start) != 0.0:
        return False
    if _orientation(first_start, first_end, second_end) != 0.0:
        return False

    def parameter(point: PointType) -> float:
        return (
            (point[0] - first_start[0]) * dx + (point[1] - first_start[1]) * dy
        ) / length_squared

    second_lo = min(parameter(second_start), parameter(second_end))
    second_hi = max(parameter(second_start), parameter(second_end))
    overlap_lo = max(0.0, second_lo)
    overlap_hi = min(1.0, second_hi)
    return overlap_hi > overlap_lo


def _arc_parameters(base: Sequence[PointType]) -> dict[PointType, float]:
    """Locate every base point by its accumulated arc length from the start."""
    parameters = {base[0]: 0.0}
    accumulated = 0.0
    for index in range(len(base) - 1):
        start = base[index]
        end = base[index + 1]
        accumulated += math.hypot(end[0] - start[0], end[1] - start[1])
        parameters[end] = accumulated
    return parameters


def _parameter_on_base(
    base: Sequence[PointType],
    parameters: dict[PointType, float],
    point: PointType,
) -> float | None:
    """Project a point onto the base polyline and return its arc length parameter."""
    for index in range(len(base) - 1):
        start = base[index]
        end = base[index + 1]
        if _orientation(start, end, point) != 0.0:
            continue
        if not (
            min(start[0], end[0]) <= point[0] <= max(start[0], end[0])
            and min(start[1], end[1]) <= point[1] <= max(start[1], end[1])
        ):
            continue
        offset = math.hypot(point[0] - start[0], point[1] - start[1])
        return parameters[start] + offset
    return None


def _merge_shared_runs(runs: Sequence[Sequence[PointType]]) -> tuple[PointType, ...]:
    """Merge differently sampled registrations of one shared edge into one line."""
    reference = max(runs, key=len)
    reference_points = list(reference)
    oriented_runs: list[list[PointType]] = []
    for run in runs:
        points = list(run)
        if _is_reversed(points, reference_points):
            points.reverse()
        oriented_runs.append(points)
    base = max(oriented_runs, key=len)
    all_points = {point for run in oriented_runs for point in run}
    parameters = _arc_parameters(base)
    located = {
        point: _parameter_on_base(base, parameters, point) for point in all_points
    }
    if any(parameter is None for parameter in located.values()):
        return tuple(reference)
    ordered = sorted(all_points, key=lambda point: cast("float", located[point]))
    if reference[0] == reference[-1]:
        ordered.append(ordered[0])
    return tuple(ordered)


def _is_reversed(run: Sequence[PointType], canonical: Sequence[PointType]) -> bool:
    """Check whether ``run`` traverses the shared geometry opposite to canonical."""
    if canonical[0] == canonical[-1]:
        ring_size = len(canonical) - 1
        first_index = canonical.index(run[0])
        second_index = canonical.index(run[1])
        return second_index != (first_index + 1) % ring_size
    return not (run[0] == canonical[0] and run[-1] == canonical[-1])


def _edge_key(run: Sequence[PointType]) -> frozenset[PointType]:
    """Build a direction independent key for one registered shared edge run."""
    if run[0] == run[-1]:
        return frozenset(run)
    return frozenset((run[0], run[-1]))


def _ring_stretches(
    ring: LineType,
    segment_neighbors: Sequence[set[int]],
) -> list[tuple[tuple[PointType, ...], bool]]:
    """Cut a ring into successive shared and private stretches."""
    points: tuple[PointType, ...] = tuple(ring)
    segment_count = len(points) - 1
    cuts = _cut_vertices(segment_neighbors)
    if not cuts:
        if not any(segment_neighbors):
            return [(points, False)]
        cuts = [0]
    extended = points + points[1:-1]
    ordered_cuts = [*cuts, cuts[0] + segment_count]
    stretches: list[tuple[tuple[PointType, ...], bool]] = []
    for cut, next_cut in pairwise(ordered_cuts):
        run = tuple(extended[cut : next_cut + 1])
        is_shared = bool(segment_neighbors[cut % segment_count])
        stretches.append((run, is_shared))
    return stretches


def _shared_neighbors(
    rings: Sequence[LineType],
    ring_polygon: Sequence[int],
) -> list[list[set[int]]]:
    """For every ring segment report the other polygons covering the same boundary."""
    segments: list[list[tuple[PointType, PointType]]] = [
        list(pairwise(ring)) for ring in rings
    ]
    neighbors: list[list[set[int]]] = [
        [set() for _ in ring_segments] for ring_segments in segments
    ]
    polygon_count = max(ring_polygon, default=-1) + 1
    for first_polygon in range(polygon_count):
        first_rings = [
            ring_index
            for ring_index, owner in enumerate(ring_polygon)
            if owner == first_polygon
        ]
        for second_polygon in range(first_polygon + 1, polygon_count):
            second_rings = [
                ring_index
                for ring_index, owner in enumerate(ring_polygon)
                if owner == second_polygon
            ]
            for first_ring in first_rings:
                for first_index, first_segment in enumerate(segments[first_ring]):
                    for second_ring in second_rings:
                        for second_index, second_segment in enumerate(
                            segments[second_ring],
                        ):
                            if _collinear_overlap(
                                *first_segment,
                                *second_segment,
                            ):
                                neighbors[first_ring][first_index].add(second_polygon)
                                neighbors[second_ring][second_index].add(
                                    first_polygon,
                                )
    return neighbors


def _cut_vertices(segment_neighbors: Sequence[set[int]]) -> list[int]:
    """Return ring vertices where the covering neighbours change."""
    segment_count = len(segment_neighbors)
    return [
        vertex
        for vertex in range(segment_count)
        if segment_neighbors[vertex] != segment_neighbors[(vertex - 1) % segment_count]
    ]


def _build_ring_entries(
    rings: Sequence[LineType],
    ring_polygon: Sequence[int],
    tolerance: float,
    preserve: Iterable[PointType],
) -> tuple[list[list[_RingEntry]], dict[frozenset[PointType], _Simplifiable]]:
    """Split every ring into shared edges and private stretches."""
    neighbors = _shared_neighbors(rings, ring_polygon)
    shared_runs: dict[frozenset[PointType], list[Sequence[PointType]]] = {}
    ring_runs: list[list[tuple[tuple[PointType, ...], bool]]] = []
    for ring, segment_neighbors in zip(rings, neighbors, strict=True):
        stretches = _ring_stretches(ring, segment_neighbors)
        for run, is_shared in stretches:
            if is_shared:
                shared_runs.setdefault(_edge_key(run), []).append(run)
        ring_runs.append(stretches)
    edges = {
        key: _Simplifiable(merged)
        for key, runs in shared_runs.items()
        if (merged := _merge_shared_runs(runs))
    }
    for edge in edges.values():
        edge.simplify(tolerance, preserve)
    ring_entries: list[list[_RingEntry]] = []
    for stretches in ring_runs:
        entries: list[_RingEntry] = []
        for run, is_shared in stretches:
            if not is_shared:
                unit = _Simplifiable(run)
                unit.simplify(tolerance, preserve)
                entries.append(_RingEntry(unit=unit, reversed=False))
                continue
            edge = edges[_edge_key(run)]
            entries.append(
                _RingEntry(unit=edge, reversed=_is_reversed(run, edge.original)),
            )
        ring_entries.append(entries)
    return ring_entries, edges


def _assemble_ring(
    entries: Sequence[_RingEntry],
    start: PointType,
) -> tuple[PointType, ...]:
    """Stitch the stretches of a ring back into a closed coordinate sequence."""
    assembled: list[PointType] = []
    for entry in entries:
        points = list(entry.unit.points)
        if entry.reversed:
            points.reverse()
        if assembled and points and assembled[-1] != points[0]:
            matching = next(
                (index for index, point in enumerate(points) if point == assembled[-1]),
                None,
            )
            if matching is not None:
                points = points[matching:]
        if assembled and points and assembled[-1] == points[0]:
            assembled.extend(points[1:])
        else:
            assembled.extend(points)
    if not assembled:
        return ()
    if assembled[-1] != assembled[0]:
        assembled.append(assembled[0])
    start_index = next(
        (index for index, point in enumerate(assembled[:-1]) if point == start),
        0,
    )
    rotated = assembled[start_index:-1] + assembled[:start_index]
    rotated.append(rotated[0])
    return tuple(rotated)


def _restore_until_simple(
    ring_entries: Sequence[Sequence[_RingEntry]],
    ring_starts: Sequence[PointType],
    units: Iterable[_Simplifiable],
) -> None:
    """Put points back until no simplified ring crosses or overlaps itself."""
    unique_units: list[_Simplifiable] = []
    for unit in units:
        if all(unit is not existing for existing in unique_units):
            unique_units.append(unit)
    while any(
        not is_simple_ring(_assemble_ring(entries, start))
        for entries, start in zip(ring_entries, ring_starts, strict=True)
    ):
        best_unit: _Simplifiable | None = None
        best_distance = -1.0
        best_index = -1
        for unit in unique_units:
            distance, index = unit.removal_priority()
            if index >= 0 and distance > best_distance:
                best_distance = distance
                best_index = index
                best_unit = unit
        if best_unit is None:
            return
        best_unit.restore(best_index)


def simplify_polygons(
    polygons: Sequence[PolygonType],
    tolerance: float,
    preserve: Iterable[PointType] | None = None,
) -> tuple[PolygonType, ...]:
    """
    Simplify the boundaries of adjacent polygons while keeping their topology.

    Boundaries shared between two polygons are identified even when one side
    carries additional sampling points or registers the boundary in the
    opposite direction. Each shared boundary is simplified exactly once, so
    neighbouring polygons keep identical, reversely oriented coordinate
    sequences along their common edge. Coordinates listed in ``preserve``
    (survey markers) are never removed.

    Private stretches are simplified with the same Douglas-Peucker tolerance.
    If a replacement chord would make a ring self-intersecting, removed points
    are restored one by one until every ring is simple again.
    """
    landmarks = tuple(preserve or ())
    rings: list[LineType] = []
    ring_polygon: list[int] = []
    ring_owner: list[tuple[int, int]] = []
    for polygon_index, polygon in enumerate(polygons):
        shell = polygon[0]
        holes = polygon[1] if len(polygon) > 1 else ()
        for ring_index, ring in enumerate((shell, *holes)):
            rings.append(ring)
            ring_polygon.append(polygon_index)
            ring_owner.append((polygon_index, ring_index))
    ring_entries, edges = _build_ring_entries(
        rings,
        ring_polygon,
        tolerance,
        landmarks,
    )
    private_units = [
        entry.unit
        for entries in ring_entries
        for entry in entries
        if all(entry.unit is not edge for edge in edges.values())
    ]
    ring_starts = [ring[0] for ring in rings]
    _restore_until_simple(
        ring_entries,
        ring_starts,
        (*edges.values(), *private_units),
    )
    simplified_rings = {
        owner: _assemble_ring(entries, start)
        for owner, entries, start in zip(
            ring_owner,
            ring_entries,
            ring_starts,
            strict=True,
        )
    }
    result: list[PolygonType] = []
    for polygon_index, polygon in enumerate(polygons):
        simplified = cast(
            "PolygonType",
            tuple(
                cast("LineType", tuple(simplified_rings[(polygon_index, ring_index)]))
                for ring_index in range(len(polygon))
            ),
        )
        result.append(simplified)
    return tuple(result)


__all__ = ["simplify_polygons"]
