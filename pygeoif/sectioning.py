#
#   Copyright (C) 2012 - 2026  Christian Ledermann
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
"""Station/offset positioning of sampling sections along a baseline.

Inspectors record a sampling cross-section (a "section") as the distance
travelled along a baseline (the station or chainage, in metres) together with
a signed offset to the left of the direction of travel.  This module turns
those records into coordinates and allows an arbitrary coordinate to look the
originating section back up.

The baseline may be a :class:`~pygeoif.geometry.LineString` or a closed
:class:`~pygeoif.geometry.LinearRing`.  It is allowed to fold back on itself,
so the same coordinate region may be covered by two neighbouring branches
that carry different chainage ranges.  The geometrically nearest point of a
coordinate can therefore sit on the wrong branch.  To avoid silently jumping
to that branch, every registered section keeps the *chainage window* in which
its record is tenable and a lookup returns all sections whose windows can
explain the coordinate.

At a turn vertex the baseline has two tangents, the incoming and the outgoing
segment.  Which one applies is a business convention modelled by
:class:`Tangent`.
"""

import math
from bisect import bisect_right
from dataclasses import dataclass
from enum import Enum
from typing import Generic
from typing import TypeVar

from pygeoif.exceptions import DimensionError
from pygeoif.geometry import LinearRing
from pygeoif.geometry import LineString
from pygeoif.geometry import Point
from pygeoif.types import GeoType
from pygeoif.types import LineType
from pygeoif.types import Point2D
from pygeoif.types import PointType

SectionIdT = TypeVar("SectionIdT")


class Tangent(str, Enum):
    """Tangent convention used at a turn vertex.

    ``INCOMING`` uses the segment arriving at the vertex, ``OUTGOING`` the
    segment leaving it.  The enum inherits from :class:`str`, so the plain
    strings ``"incoming"`` and ``"outgoing"`` are accepted too.
    """

    INCOMING = "incoming"
    OUTGOING = "outgoing"


@dataclass(frozen=True, slots=True)
class NearestPoint:
    """Projection of a coordinate onto one baseline segment.

    Attributes
    ----------
    point: Point
        The projected point on the baseline.
    distance: float
        Unsigned Euclidean distance between the queried coordinate and
        ``point``.
    side: float
        Signed perpendicular distance, positive to the left of the segment's
        direction of travel.
    along: float
        Signed residual along the segment direction between the projection
        and the queried coordinate.
    chainage: float
        Chainage of ``point`` measured along the baseline.
    segment_index: int
        Index of the segment carrying the projection.
    """

    point: Point
    distance: float
    side: float
    along: float
    chainage: float
    segment_index: int


@dataclass(frozen=True, slots=True)
class Section(Generic[SectionIdT]):
    """A sampling cross-section reconstructed from a survey record.

    Attributes
    ----------
    identifier:
        The business identifier (section number) of the record.
    chainage: float
        Distance along the baseline in metres.
    offset: float
        Signed offset to the left of the direction of travel in metres.
    point: Point
        The reconstructed coordinate of the section.
    segment_index: int
        Segment whose tangent the offset is measured against.
    tangent: Tangent
        Tangent convention that applies when the station is a turn vertex.
    """

    identifier: SectionIdT
    chainage: float
    offset: float
    point: Point
    segment_index: int
    tangent: Tangent


@dataclass(frozen=True, slots=True)
class Match(Generic[SectionIdT]):
    """A section that can explain a queried coordinate.

    Attributes
    ----------
    section: Section
        The matching section.
    distance: float
        Unsigned distance between the query and the section's best explanation.
    side: float
        Signed perpendicular distance, positive to the left.
    along: float
        Signed residual along the direction of travel.
    """

    section: Section[SectionIdT]
    distance: float
    side: float
    along: float


@dataclass(frozen=True, slots=True)
class _Segment:
    """A straight baseline segment and its chainage window."""

    start: Point2D
    end: Point2D
    start_chainage: float
    end_chainage: float
    length: float
    unit: Point2D
    normal: Point2D


class BaselineSectioner(Generic[SectionIdT]):
    """Turn station/offset records into coordinates and look them back up.

    Parameters
    ----------
    baseline: LineString
        The survey baseline.  A :class:`LinearRing` is treated as a closed
        baseline whose chainage runs around the ring; only 2D baselines are
        supported.
    tangent: Tangent or str, optional
        Default tangent convention at turn vertices.  Defaults to
        :attr:`Tangent.OUTGOING`.
    tolerance: float, optional
        Numerical tolerance in metres used for chainage and distance
        comparisons.  Defaults to ``1e-9``.
    """

    def __init__(
        self,
        baseline: GeoType,
        *,
        tangent: Tangent | str = Tangent.OUTGOING,
        tolerance: float = 1e-9,
    ) -> None:
        if not isinstance(baseline, LineString):
            msg = "baseline must be a LineString or LinearRing"
            raise TypeError(msg)
        if baseline.has_z:
            msg = "Station/offset positioning is only implemented for 2D baselines"
            raise DimensionError(msg)
        if baseline.is_empty or len(baseline.coords) < 2:  # noqa: PLR2004
            msg = "baseline must contain at least two distinct points"
            raise ValueError(msg)
        if not math.isfinite(tolerance) or tolerance < 0:
            msg = "tolerance must be a finite non-negative number"
            raise ValueError(msg)
        self._baseline = baseline
        self._tolerance = tolerance
        self._default_tangent = self._as_tangent(tangent)
        self._closed = isinstance(baseline, LinearRing)
        self._segments, self._cumulative = self._build_segments(baseline.coords)
        self._sections: dict[SectionIdT, Section[SectionIdT]] = {}

    @property
    def baseline(self) -> LineString:
        """Return the baseline geometry."""
        return self._baseline

    @property
    def length(self) -> float:
        """Return the total baseline length in metres."""
        return self._cumulative[-1]

    @property
    def tangent(self) -> Tangent:
        """Return the default tangent convention at turn vertices."""
        return self._default_tangent

    @property
    def sections(self) -> tuple[Section[SectionIdT], ...]:
        """Return all registered sections ordered by chainage."""
        return tuple(
            sorted(self._sections.values(), key=lambda section: section.chainage),
        )

    def __len__(self) -> int:
        """Return the number of registered sections."""
        return len(self._sections)

    def __contains__(self, identifier: object) -> bool:
        """Return whether a section with the identifier is registered."""
        return identifier in self._sections

    def _build_segments(
        self,
        coords: LineType,
    ) -> tuple[tuple[_Segment, ...], tuple[float, ...]]:
        """Split the baseline into unit-vector segments with cumulative lengths."""
        closed_ring = self._closed and coords[0] == coords[-1]
        points = coords[:-1] if closed_ring else coords
        segments: list[_Segment] = []
        cumulative = [0.0]
        chainage = 0.0
        edges = (
            tuple(zip(points, (*points[1:], points[0]), strict=True))
            if self._closed
            else tuple(zip(points, points[1:], strict=False))
        )
        for start, end in edges:
            dx = end[0] - start[0]
            dy = end[1] - start[1]
            length = math.hypot(dx, dy)
            if length <= self._tolerance:
                msg = "baseline segments must have a positive length"
                raise ValueError(msg)
            chainage += length
            segments.append(
                _Segment(
                    start=(start[0], start[1]),
                    end=(end[0], end[1]),
                    start_chainage=cumulative[-1],
                    end_chainage=chainage,
                    length=length,
                    unit=(dx / length, dy / length),
                    normal=(-dy / length, dx / length),
                ),
            )
            cumulative.append(chainage)
        return tuple(segments), tuple(cumulative)

    @staticmethod
    def _as_tangent(tangent: Tangent | str) -> Tangent:
        """Coerce a string to the Tangent enum."""
        if isinstance(tangent, Tangent):
            return tangent
        try:
            return Tangent(tangent)
        except ValueError as err:
            msg = "tangent must be 'incoming' or 'outgoing'"
            raise ValueError(msg) from err

    @staticmethod
    def _as_xy(coordinate: PointType | Point) -> Point2D:
        """Return the 2D coordinate pair of a point or coordinate tuple."""
        if isinstance(coordinate, Point):
            if coordinate.has_z:
                msg = "Only 2D coordinates can be located on a 2D baseline"
                raise DimensionError(msg)
            return coordinate.x, coordinate.y
        if len(coordinate) != 2:  # noqa: PLR2004
            msg = "Only 2D coordinates can be located on a 2D baseline"
            raise DimensionError(msg)
        return float(coordinate[0]), float(coordinate[1])

    def _resolve(
        self,
        chainage: float,
        tangent: Tangent | str | None,
    ) -> tuple[int, float, Tangent]:
        """Resolve a station to a segment, segment parameter and convention."""
        total = self.length
        if not math.isfinite(chainage) or not (
            -self._tolerance <= chainage <= total + self._tolerance
        ):
            msg = f"chainage must be between 0 and {total}"
            raise ValueError(msg)
        convention = (
            self._default_tangent
            if tangent is None
            else self._as_tangent(tangent)
        )
        station = min(total, max(0.0, chainage))
        endpoint = self._resolve_endpoint(station, convention)
        if endpoint is not None:
            return endpoint
        index = min(
            bisect_right(self._cumulative, station) - 1,
            len(self._segments) - 1,
        )
        segment = self._segments[index]
        parameter = (station - segment.start_chainage) / segment.length
        vertex = self._resolve_vertex(index, parameter, convention)
        if vertex is not None:
            return vertex
        return index, min(1.0, max(0.0, parameter)), convention

    def _resolve_endpoint(
        self,
        station: float,
        convention: Tangent,
    ) -> tuple[int, float, Tangent] | None:
        """Resolve a station coinciding with a baseline endpoint."""
        if math.isclose(station, 0.0, abs_tol=self._tolerance):
            return self._resolve_origin(convention)
        if self._closed or not math.isclose(
            station,
            self.length,
            abs_tol=self._tolerance,
        ):
            return None
        if convention is Tangent.OUTGOING:
            msg = "No outgoing segment at the end of an open baseline"
            raise ValueError(msg)
        return len(self._segments) - 1, 1.0, convention

    def _resolve_vertex(
        self,
        index: int,
        parameter: float,
        convention: Tangent,
    ) -> tuple[int, float, Tangent] | None:
        """Resolve a station on a turn vertex under the tangent convention."""
        segment_count = len(self._segments)
        has_previous = index > 0 or self._closed
        at_start = parameter <= self._tolerance
        if at_start and convention is Tangent.INCOMING and has_previous:
            return (index - 1) % segment_count, 1.0, convention
        at_end = parameter >= 1.0 - self._tolerance
        has_next = index + 1 < segment_count or self._closed
        if at_end and convention is Tangent.OUTGOING and has_next:
            return (index + 1) % segment_count, 0.0, convention
        return None

    def _resolve_origin(self, convention: Tangent) -> tuple[int, float, Tangent]:
        """Resolve the zero chainage point of an open or closed baseline."""
        if convention is Tangent.OUTGOING:
            return 0, 0.0, convention
        if not self._closed:
            msg = "No incoming segment at the start of an open baseline"
            raise ValueError(msg)
        return len(self._segments) - 1, 1.0, convention

    def locate(
        self,
        chainage: float,
        offset: float,
        tangent: Tangent | str | None = None,
    ) -> Point:
        """Return the coordinate of a station/offset record.

        A positive offset places the point to the left of the direction of
        travel, a negative offset to the right.
        """
        if not math.isfinite(offset):
            msg = "offset must be a finite number"
            raise ValueError(msg)
        index, parameter, _convention = self._resolve(chainage, tangent)
        segment = self._segments[index]
        base_x = segment.start[0] + parameter * (
            segment.end[0] - segment.start[0]
        )
        base_y = segment.start[1] + parameter * (
            segment.end[1] - segment.start[1]
        )
        return Point(
            base_x + offset * segment.normal[0],
            base_y + offset * segment.normal[1],
        )

    def add_section(
        self,
        identifier: SectionIdT,
        chainage: float,
        offset: float,
        tangent: Tangent | str | None = None,
    ) -> Section[SectionIdT]:
        """Register a survey record and return the reconstructed section.

        The section keeps its tenable chainage window so that later coordinate
        lookups cannot silently match it against another baseline branch.
        Windows are split at the midpoint between neighbouring section
        chainages.
        """
        if identifier in self._sections:
            msg = f"Section {identifier!r} is already registered"
            raise ValueError(msg)
        index, parameter, convention = self._resolve(chainage, tangent)
        point = self.locate(chainage, offset, convention)
        station = min(self.length, max(0.0, chainage))
        if self._closed and math.isclose(
            station,
            self.length,
            abs_tol=self._tolerance,
        ):
            station = 0.0
        section = Section(
            identifier=identifier,
            chainage=station,
            offset=offset,
            point=point,
            segment_index=index,
            tangent=convention,
        )
        self._sections[identifier] = section
        return section

    def section(self, identifier: SectionIdT) -> Section[SectionIdT]:
        """Return the section registered under ``identifier``."""
        try:
            return self._sections[identifier]
        except KeyError:
            msg = f"No section registered under {identifier!r}"
            raise KeyError(msg) from None

    def _chainages(self) -> list[float]:
        """Return the sorted, distinct chainages of all sections."""
        return sorted({section.chainage for section in self._sections.values()})

    def _section_window(self, chainage: float) -> tuple[float, float]:
        """Return the tenable chainage window for a section chainage."""
        return self._window_for(chainage, self._chainages())

    def _window_for(
        self,
        chainage: float,
        chainages: list[float],
    ) -> tuple[float, float]:
        """Compute the window for ``chainage`` from sorted distinct chainages."""
        if len(chainages) <= 1:  # noqa: PLR2004
            return 0.0, self.length
        position = bisect_right(chainages, chainage) - 1
        if self._closed:
            previous = chainages[position - 1]
            following = chainages[(position + 1) % len(chainages)]
            low = self._circular_midpoint(previous, chainage)
            high = self._circular_midpoint(chainage, following)
            return low, high
        low = (
            0.0
            if position == 0  # noqa: PLR2004
            else (chainages[position - 1] + chainage) / 2.0
        )
        if position + 1 >= len(chainages):
            high = self.length
        else:
            high = (chainage + chainages[position + 1]) / 2.0
        return low, high

    def _circular_midpoint(self, low: float, high: float) -> float:
        """Return the midpoint on a circular chainage axis."""
        if high < low:
            high += self.length
        midpoint = (low + high) / 2.0
        return midpoint % self.length if self._closed else midpoint

    def _in_window(
        self,
        window: tuple[float, float],
        chainage: float,
    ) -> bool:
        """Return whether a projection chainage falls inside a window."""
        low, high = window
        if low <= high:
            return low - self._tolerance <= chainage <= high + self._tolerance
        return (
            chainage >= low - self._tolerance
            or chainage <= high + self._tolerance
        )

    def _project(
        self,
        point: Point2D,
        segment: _Segment,
        segment_index: int,
    ) -> NearestPoint:
        """Project a coordinate onto a single baseline segment."""
        wx = point[0] - segment.start[0]
        wy = point[1] - segment.start[1]
        unit_x, unit_y = segment.unit
        parameter = min(
            1.0,
            max(0.0, (wx * unit_x + wy * unit_y) / segment.length),
        )
        projection_x = segment.start[0] + parameter * segment.length * unit_x
        projection_y = segment.start[1] + parameter * segment.length * unit_y
        delta_x = point[0] - projection_x
        delta_y = point[1] - projection_y
        return NearestPoint(
            point=Point(projection_x, projection_y),
            distance=math.hypot(delta_x, delta_y),
            side=wy * unit_x - wx * unit_y,
            along=delta_x * unit_x + delta_y * unit_y,
            chainage=segment.start_chainage + parameter * segment.length,
            segment_index=segment_index,
        )

    def nearest_points(
        self,
        coordinate: PointType | Point,
    ) -> tuple[NearestPoint, ...]:
        """Project a coordinate onto every segment, nearest first.

        A turn vertex of a closed ring yields two projections with different
        chainages (the incoming and the outgoing one); both are kept.
        """
        point = self._as_xy(coordinate)
        projections = tuple(
            self._project(point, segment, index)
            for index, segment in enumerate(self._segments)
        )
        return tuple(
            sorted(projections, key=lambda projection: projection.distance),
        )

    def nearest_point(self, coordinate: PointType | Point) -> NearestPoint:
        """Return the single nearest projection of a coordinate."""
        return self.nearest_points(coordinate)[0]

    def find_sections(
        self,
        coordinate: PointType | Point,
        max_distance: float | None = None,
    ) -> tuple[Match[SectionIdT], ...]:
        """Return every section whose window can explain the coordinate.

        A section is scored by the smaller of the nearest baseline projection
        inside its tenable chainage window and the straight distance to its own
        reconstructed point.  The first term prevents a nearer projection on a
        folded parallel branch from stealing the lookup, while the second
        guarantees that a coordinate reconstructed from a section looks that
        same section back up even when its offset pushes the nearest projection
        into another chainage window.  Sections on different branches are
        returned together whenever both remain tenable, ordered by distance.
        ``max_distance`` limits the accepted distance in metres.
        """
        if max_distance is not None and (
            not math.isfinite(max_distance) or max_distance < 0
        ):
            msg = "max_distance must be a finite non-negative number"
            raise ValueError(msg)
        point = self._as_xy(coordinate)
        projections = tuple(
            self._project(point, segment, index)
            for index, segment in enumerate(self._segments)
        )
        chainages = self._chainages()
        matches: list[Match[SectionIdT]] = []
        for section in self.sections:
            window = self._window_for(section.chainage, chainages)
            candidates = [
                projection
                for projection in projections
                if self._in_window(window, projection.chainage)
            ]
            best = (
                min(candidates, key=lambda projection: projection.distance)
                if candidates
                else None
            )
            direct = self._direct_residual(point, section)
            if best is None or direct.distance < best.distance:
                best = direct
            if max_distance is not None and best.distance > max_distance + (
                self._tolerance
            ):
                continue
            matches.append(
                Match(
                    section=section,
                    distance=best.distance,
                    side=best.side,
                    along=best.along,
                ),
            )
        matches.sort(
            key=lambda match: (
                match.distance,
                math.hypot(
                    point[0] - match.section.point.x,
                    point[1] - match.section.point.y,
                ),
            ),
        )
        return tuple(matches)

    def _direct_residual(
        self,
        point: Point2D,
        section: Section[SectionIdT],
    ) -> NearestPoint:
        """Return the residual of a query against a section's own point."""
        segment = self._segments[section.segment_index]
        delta_x = point[0] - section.point.x
        delta_y = point[1] - section.point.y
        return NearestPoint(
            point=section.point,
            distance=math.hypot(delta_x, delta_y),
            side=delta_x * segment.normal[0] + delta_y * segment.normal[1],
            along=delta_x * segment.unit[0] + delta_y * segment.unit[1],
            chainage=section.chainage,
            segment_index=section.segment_index,
        )

    def find_section(
        self,
        coordinate: PointType | Point,
        max_distance: float | None = None,
    ) -> Section[SectionIdT] | None:
        """Return the nearest matching section, or ``None`` if none matches."""
        matches = self.find_sections(coordinate, max_distance)
        return matches[0].section if matches else None


__all__ = [
    "BaselineSectioner",
    "Match",
    "NearestPoint",
    "Section",
    "Tangent",
]
