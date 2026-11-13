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
"""
Auditable geometric identity comparison.

The same parcel of land can be transcribed in many ways: a surveyor may
start copying a ring at any vertex, traverse it in either direction and
list the holes in any order.  A plain coordinate-by-coordinate comparison
(such as ``__eq__``) treats those transcriptions as different objects, so
a ledger would happily record the same parcel twice.

This module provides :func:`compare_identity`, which decides whether two
geometries describe the *same shape*:

* rings are compared up to cyclic rotation (any starting vertex) and
  reversal (either traversal direction),
* the interior rings (holes) of a polygon may be listed in any order,
* the parts of a multi-part geometry or collection may be listed in any
  order.

An optional ``grid_size`` quantizes coordinates to a regular grid before
comparing, which absorbs rounding noise introduced by re-transcription.
Quantization is lossy: if it collapses a ring - for example a narrow hole
that is pressed into a line - the comparison reports
``IdentityVerdict.UNDECIDABLE`` instead of declaring the geometries
identical on the basis of destroyed information.

Every result is auditable: the returned :class:`IdentityMatch` explains
the verdict in plain language and, for identical geometries, describes
how the rings and vertices of the two geometries correspond.  If repeated
vertices or repeated rings allow several correspondences, all of them
(up to ``MAX_CORRESPONDENCES``) are reported.
"""

import math
from dataclasses import dataclass
from enum import Enum
from typing import cast

from pygeoif.factories import shape
from pygeoif.functions import signed_area
from pygeoif.geometry import Geometry
from pygeoif.geometry import GeometryCollection
from pygeoif.geometry import LinearRing
from pygeoif.geometry import LineString
from pygeoif.geometry import MultiLineString
from pygeoif.geometry import MultiPoint
from pygeoif.geometry import MultiPolygon
from pygeoif.geometry import Point
from pygeoif.geometry import Polygon
from pygeoif.types import GeoCollectionInterface
from pygeoif.types import GeoCollectionType
from pygeoif.types import GeoInterface
from pygeoif.types import GeoType
from pygeoif.types import LineType
from pygeoif.types import PointType

MAX_CORRESPONDENCES = 64
"""Maximum number of correspondences reported by :func:`compare_identity`."""

MIN_RING_VERTICES = 3
"""Minimum number of distinct vertices for a non-degenerate ring."""

RingPath = tuple[str | int, ...]
"""A path identifying a ring (or line, or point) inside a geometry."""

_QRing = tuple[tuple[float, ...], ...]


class IdentityVerdict(str, Enum):
    """The outcome of an identity comparison."""

    IDENTICAL = "identical"
    DIFFERENT = "different"
    UNDECIDABLE = "undecidable"


@dataclass(frozen=True)
class VertexAlignment:
    """
    One possible vertex-to-vertex correspondence between two rings.

    Vertices are indexed along the ring *without* the duplicated closing
    vertex.  ``offset`` is the rotation applied to the second ring and
    ``is_reversed`` tells whether the second ring is traversed in the
    opposite direction: vertex ``i`` of the first ring corresponds to
    vertex ``(offset + i) % n`` of the second ring, or to vertex
    ``(offset - i) % n`` when ``is_reversed`` is true.  ``vertex_map``
    spells out these ``(first, second)`` index pairs explicitly.
    """

    offset: int
    is_reversed: bool
    vertex_map: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class RingCorrespondence:
    """
    How a ring (or line, or point) of one geometry maps onto the other.

    ``first_ring`` and ``second_ring`` are paths identifying the element
    in each geometry:

    * ``("exterior",)`` or ``("interior", k)`` for a polygon,
    * ``(p, "exterior")`` or ``(p, "interior", k)`` for polygon ``p`` of
      a multi-polygon or of a collection,
    * ``("line",)`` for a linestring, ``(k, "line")`` within a
      multi-linestring,
    * ``("point",)`` for a point, ``(k, "point")`` within a multi-point.

    ``alignments`` holds every vertex correspondence consistent with the
    two elements; repeated vertices may allow several of them.
    """

    first_ring: RingPath
    second_ring: RingPath
    alignments: tuple[VertexAlignment, ...]


@dataclass(frozen=True)
class Correspondence:
    """
    One complete mapping between two identical geometries.

    ``geometry_map`` pairs the part indices of multi-part geometries
    (``(0, 2)`` means part 0 of the first geometry corresponds to part 2
    of the second); it is empty for single geometries.  ``rings``
    describes the ring and vertex correspondences of the matched parts.
    """

    geometry_map: tuple[tuple[int, int], ...]
    rings: tuple[RingCorrespondence, ...]


@dataclass(frozen=True)
class IdentityMatch:
    """
    The auditable result of :func:`compare_identity`.

    ``correspondences`` is empty unless the verdict is
    ``IdentityVerdict.IDENTICAL``.  ``reasons`` always explains the
    outcome in plain language.  ``truncated`` is true when more
    correspondences exist than are reported.
    """

    verdict: IdentityVerdict
    grid_size: float | None
    correspondences: tuple[Correspondence, ...]
    reasons: tuple[str, ...]
    truncated: bool = False

    def __bool__(self) -> bool:
        """Return True only when the two geometries are identical."""
        return self.verdict is IdentityVerdict.IDENTICAL


@dataclass
class _Pairwise:
    """Internal result of a recursive comparison."""

    identical: bool
    correspondences: tuple[Correspondence, ...]
    reasons: tuple[str, ...]
    truncated: bool = False


def _quantize_coordinate(
    coordinate: PointType,
    grid_size: float | None,
) -> tuple[float, ...]:
    if grid_size is None:
        return tuple(float(c) for c in coordinate)
    return tuple(float(round(c / grid_size)) for c in coordinate)


def _open_ring(coords: LineType) -> tuple[PointType, ...]:
    """Return the ring vertices without the duplicated closing vertex."""
    points = tuple(coords)
    if len(points) > 1 and points[0] == points[-1]:
        points = points[:-1]
    return cast("tuple[PointType, ...]", points)


def _point_coordinate(point: Point) -> PointType:
    """Return the coordinate of a non-empty point."""
    return cast("tuple[PointType]", point.coords)[0]


def _quantize_ring(coords: LineType, grid_size: float | None) -> _QRing:
    return tuple(
        _quantize_coordinate(coordinate, grid_size)
        for coordinate in _open_ring(coords)
    )


def _ring_alignments(first: _QRing, second: _QRing) -> tuple[VertexAlignment, ...]:
    """Return every rotation/reversal mapping the first ring onto the second."""
    count = len(first)
    if count == 0 or count != len(second):
        return ()
    alignments: list[VertexAlignment] = []
    seen: set[tuple[tuple[int, int], ...]] = set()
    for offset in range(count):
        for reverse in (False, True):
            vertex_map = tuple(
                (
                    i,
                    (offset - i) % count if reverse else (offset + i) % count,
                )
                for i in range(count)
            )
            if vertex_map in seen:
                continue
            seen.add(vertex_map)
            if all(first[i] == second[j] for i, j in vertex_map):
                alignments.append(
                    VertexAlignment(
                        offset=offset,
                        is_reversed=reverse,
                        vertex_map=vertex_map,
                    ),
                )
    return tuple(alignments)


def _enumerate_bijections(
    compatible: list[list[bool]],
) -> tuple[list[tuple[int, ...]], bool]:
    """
    Enumerate one-to-one matchings between two equally sized parts lists.

    Returns the matchings (each a tuple mapping first indices to second
    indices) and whether the enumeration was truncated at
    ``MAX_CORRESPONDENCES``.
    """
    count = len(compatible)
    matchings: list[tuple[int, ...]] = []
    used = [False] * count
    permutation = [-1] * count
    truncated = False

    def backtrack(index: int) -> None:
        nonlocal truncated
        if truncated:
            return
        if index == count:
            matchings.append(tuple(permutation))
            truncated = len(matchings) >= MAX_CORRESPONDENCES
            return
        for candidate in range(count):
            if not used[candidate] and compatible[index][candidate]:
                used[candidate] = True
                permutation[index] = candidate
                backtrack(index + 1)
                used[candidate] = False
                permutation[index] = -1

    backtrack(0)
    return matchings, truncated


def _compare_points(
    first: Point,
    second: Point,
    grid_size: float | None,
) -> _Pairwise:
    point1 = _quantize_coordinate(_point_coordinate(first), grid_size)
    point2 = _quantize_coordinate(_point_coordinate(second), grid_size)
    if point1 != point2:
        return _Pairwise(
            identical=False,
            correspondences=(),
            reasons=(f"Points differ: {point1} and {point2}.",),
        )
    alignment = VertexAlignment(
        offset=0,
        is_reversed=False,
        vertex_map=((0, 0),),
    )
    correspondence = Correspondence(
        geometry_map=(),
        rings=(
            RingCorrespondence(
                first_ring=("point",),
                second_ring=("point",),
                alignments=(alignment,),
            ),
        ),
    )
    return _Pairwise(
        identical=True,
        correspondences=(correspondence,),
        reasons=(f"Points coincide at {point1}.",),
    )


def _compare_lines(
    first: LineString,
    second: LineString,
    grid_size: float | None,
) -> _Pairwise:
    line1 = tuple(_quantize_coordinate(c, grid_size) for c in first.coords)
    line2 = tuple(_quantize_coordinate(c, grid_size) for c in second.coords)
    alignments: list[VertexAlignment] = []
    count = len(line1)
    if count == len(line2):
        if line1 == line2:
            alignments.append(
                VertexAlignment(
                    offset=0,
                    is_reversed=False,
                    vertex_map=tuple((i, i) for i in range(count)),
                ),
            )
        if count > 1 and line1 == line2[::-1]:
            alignments.append(
                VertexAlignment(
                    offset=0,
                    is_reversed=True,
                    vertex_map=tuple((i, count - 1 - i) for i in range(count)),
                ),
            )
    if not alignments:
        reason = (
            f"Lines have different vertex counts ({count} and {len(line2)})."
            if count != len(line2)
            else "Lines do not coincide in either direction."
        )
        return _Pairwise(identical=False, correspondences=(), reasons=(reason,))
    correspondence = Correspondence(
        geometry_map=(),
        rings=(
            RingCorrespondence(
                first_ring=("line",),
                second_ring=("line",),
                alignments=tuple(alignments),
            ),
        ),
    )
    return _Pairwise(
        identical=True,
        correspondences=(correspondence,),
        reasons=(f"Lines coincide ({len(alignments)} vertex alignment(s)).",),
    )


def _compare_rings(
    first: LinearRing,
    second: LinearRing,
    grid_size: float | None,
) -> _Pairwise:
    ring1 = _quantize_ring(first.coords, grid_size)
    ring2 = _quantize_ring(second.coords, grid_size)
    alignments = _ring_alignments(ring1, ring2)
    if not alignments:
        reason = (
            f"Rings have different vertex counts ({len(ring1)} and {len(ring2)})."
            if len(ring1) != len(ring2)
            else "Rings do not coincide under any rotation or reversal."
        )
        return _Pairwise(identical=False, correspondences=(), reasons=(reason,))
    correspondence = Correspondence(
        geometry_map=(),
        rings=(
            RingCorrespondence(
                first_ring=("ring",),
                second_ring=("ring",),
                alignments=alignments,
            ),
        ),
    )
    return _Pairwise(
        identical=True,
        correspondences=(correspondence,),
        reasons=(f"Rings coincide ({len(alignments)} vertex alignment(s)).",),
    )


def _polygon_match_reasons(
    exterior_alignments: tuple[VertexAlignment, ...],
    hole_alignments: list[list[tuple[VertexAlignment, ...]]],
    matchings: list[tuple[int, ...]],
) -> tuple[str, ...]:
    reasons = [
        f"Exterior rings coincide ({len(exterior_alignments)} vertex "
        "alignment(s)).",
    ]
    for i, j in enumerate(matchings[0]):
        reasons.append(
            f"Interior ring {i} of the first geometry corresponds to "
            f"interior ring {j} of the second geometry "
            f"({len(hole_alignments[i][j])} vertex alignment(s)).",
        )
    if len(matchings) > 1:
        reasons.append(
            f"{len(matchings)} distinct interior ring correspondences exist.",
        )
    return tuple(reasons)


def _compare_polygons(
    first: Polygon,
    second: Polygon,
    grid_size: float | None,
) -> _Pairwise:
    exterior1 = _quantize_ring(first.exterior.coords, grid_size)
    exterior2 = _quantize_ring(second.exterior.coords, grid_size)
    exterior_alignments = _ring_alignments(exterior1, exterior2)
    if not exterior_alignments:
        reason = (
            "Exterior rings have different vertex counts "
            f"({len(exterior1)} and {len(exterior2)})."
            if len(exterior1) != len(exterior2)
            else "Exterior rings do not coincide under any rotation or reversal."
        )
        return _Pairwise(identical=False, correspondences=(), reasons=(reason,))
    holes1 = [_quantize_ring(hole.coords, grid_size) for hole in first.interiors]
    holes2 = [_quantize_ring(hole.coords, grid_size) for hole in second.interiors]
    if len(holes1) != len(holes2):
        return _Pairwise(
            identical=False,
            correspondences=(),
            reasons=(
                f"Different number of interior rings: {len(holes1)} and "
                f"{len(holes2)}.",
            ),
        )
    hole_alignments = [
        [_ring_alignments(hole1, hole2) for hole2 in holes2] for hole1 in holes1
    ]
    matchings, truncated = _enumerate_bijections(
        [[bool(alignments) for alignments in row] for row in hole_alignments],
    )
    if not matchings:
        return _Pairwise(
            identical=False,
            correspondences=(),
            reasons=("The interior rings cannot be matched one-to-one.",),
        )
    correspondences = tuple(
        Correspondence(
            geometry_map=(),
            rings=(
                RingCorrespondence(
                    first_ring=("exterior",),
                    second_ring=("exterior",),
                    alignments=exterior_alignments,
                ),
                *(
                    RingCorrespondence(
                        first_ring=("interior", i),
                        second_ring=("interior", j),
                        alignments=hole_alignments[i][j],
                    )
                    for i, j in enumerate(matching)
                ),
            ),
        )
        for matching in matchings
    )
    return _Pairwise(
        identical=True,
        correspondences=correspondences,
        reasons=_polygon_match_reasons(
            exterior_alignments,
            hole_alignments,
            matchings,
        ),
        truncated=truncated,
    )


def _compare_multi(
    first: MultiPoint | MultiLineString | MultiPolygon | GeometryCollection,
    second: MultiPoint | MultiLineString | MultiPolygon | GeometryCollection,
    grid_size: float | None,
    part_noun: str,
) -> _Pairwise:
    parts1 = cast("list[Geometry | GeometryCollection]", list(first.geoms))
    parts2 = cast("list[Geometry | GeometryCollection]", list(second.geoms))
    if len(parts1) != len(parts2):
        return _Pairwise(
            identical=False,
            correspondences=(),
            reasons=(
                f"Different number of {part_noun}: {len(parts1)} and "
                f"{len(parts2)}.",
            ),
        )
    subresults: dict[tuple[int, int], _Pairwise] = {}
    compatible: list[list[bool]] = []
    for i, part1 in enumerate(parts1):
        row = []
        for j, part2 in enumerate(parts2):
            match = False
            if part1.geom_type == part2.geom_type:
                result = _compare(part1, part2, grid_size)
                if result.identical:
                    match = True
                    subresults[(i, j)] = result
            row.append(match)
        compatible.append(row)
    matchings, truncated = _enumerate_bijections(compatible)
    if not matchings:
        return _Pairwise(
            identical=False,
            correspondences=(),
            reasons=(f"The {part_noun} cannot be matched one-to-one.",),
        )
    truncated = truncated or any(r.truncated for r in subresults.values())
    correspondences = tuple(
        Correspondence(
            geometry_map=tuple(enumerate(matching)),
            rings=tuple(
                RingCorrespondence(
                    first_ring=(i, *correspondence.first_ring),
                    second_ring=(j, *correspondence.second_ring),
                    alignments=correspondence.alignments,
                )
                for i, j in enumerate(matching)
                for correspondence in subresults[(i, j)].correspondences[0].rings
            ),
        )
        for matching in matchings
    )
    reasons = [f"All {part_noun} coincide pairwise."]
    reasons.extend(
        f"Part {i} of the first geometry corresponds to part {j} of the "
        "second geometry." for i, j in enumerate(matchings[0])
    )
    if len(matchings) > 1:
        reasons.append(f"{len(matchings)} distinct part correspondences exist.")
    return _Pairwise(
        identical=True,
        correspondences=correspondences,
        reasons=tuple(reasons),
        truncated=truncated,
    )


def _compare(
    first: Geometry | GeometryCollection,
    second: Geometry | GeometryCollection,
    grid_size: float | None,
) -> _Pairwise:
    if isinstance(first, Point) and isinstance(second, Point):
        result = _compare_points(first, second, grid_size)
    elif isinstance(first, LinearRing) and isinstance(second, LinearRing):
        result = _compare_rings(first, second, grid_size)
    elif isinstance(first, LineString) and isinstance(second, LineString):
        result = _compare_lines(first, second, grid_size)
    elif isinstance(first, Polygon) and isinstance(second, Polygon):
        result = _compare_polygons(first, second, grid_size)
    elif isinstance(first, MultiPoint) and isinstance(second, MultiPoint):
        result = _compare_multi(first, second, grid_size, "points")
    elif isinstance(first, MultiLineString) and isinstance(second, MultiLineString):
        result = _compare_multi(first, second, grid_size, "lines")
    elif isinstance(first, MultiPolygon) and isinstance(second, MultiPolygon):
        result = _compare_multi(first, second, grid_size, "polygons")
    elif isinstance(first, GeometryCollection) and isinstance(
        second,
        GeometryCollection,
    ):
        result = _compare_multi(first, second, grid_size, "geometries")
    else:
        result = _Pairwise(
            identical=False,
            correspondences=(),
            reasons=(
                f"Geometry types differ: {first.geom_type} and "
                f"{second.geom_type}.",
            ),
        )
    return result


def _ring_degeneracy_reason(
    coords: LineType,
    grid_size: float,
    what: str,
) -> str | None:
    vertices = _quantize_ring(coords, grid_size)
    distinct = len(set(vertices))
    if distinct < MIN_RING_VERTICES:
        return (
            f"Quantization with grid_size={grid_size} collapses {what} to "
            f"{distinct} distinct grid point(s); identity cannot be decided."
        )
    if signed_area(cast("LineType", (*vertices, vertices[0]))) == 0:
        return (
            f"Quantization with grid_size={grid_size} collapses {what} to "
            "zero area (a line); identity cannot be decided."
        )
    return None


def _polygon_degeneracy_reasons(
    polygon: Polygon,
    grid_size: float,
    where: str,
) -> list[str]:
    reasons = []
    exterior_reason = _ring_degeneracy_reason(
        polygon.exterior.coords,
        grid_size,
        f"the exterior ring of {where}",
    )
    if exterior_reason is not None:
        reasons.append(exterior_reason)
    for k, hole in enumerate(polygon.interiors):
        hole_reason = _ring_degeneracy_reason(
            hole.coords,
            grid_size,
            f"interior ring {k} of {where}",
        )
        if hole_reason is not None:
            reasons.append(hole_reason)
    return reasons


def _linestring_degeneracy_reasons(
    line: LineString,
    grid_size: float,
    where: str,
) -> list[str]:
    points = [_quantize_coordinate(c, grid_size) for c in line.coords]
    if len(points) > 1 and len(set(points)) < 2:  # noqa: PLR2004
        return [
            f"Quantization with grid_size={grid_size} collapses the line "
            f"of {where} to a single grid point; identity cannot be decided.",
        ]
    return []


def _multipoint_degeneracy_reasons(
    multipoint: MultiPoint,
    grid_size: float,
    where: str,
) -> list[str]:
    raw_points = [_point_coordinate(point) for point in multipoint.geoms]
    quantized = [_quantize_coordinate(point, grid_size) for point in raw_points]
    if len(set(quantized)) < len(set(raw_points)):
        return [
            f"Quantization with grid_size={grid_size} merges points of "
            f"{where}; identity cannot be decided.",
        ]
    return []


def _degeneracy_reasons(
    geometry: Geometry | GeometryCollection,
    grid_size: float,
    where: str,
) -> list[str]:
    """Find rings or parts that quantization collapses or merges."""
    reasons: list[str] = []
    if isinstance(geometry, Polygon):
        reasons = _polygon_degeneracy_reasons(geometry, grid_size, where)
    elif isinstance(geometry, LinearRing):
        ring_reason = _ring_degeneracy_reason(
            geometry.coords,
            grid_size,
            f"the ring of {where}",
        )
        if ring_reason is not None:
            reasons.append(ring_reason)
    elif isinstance(geometry, LineString):
        reasons = _linestring_degeneracy_reasons(geometry, grid_size, where)
    elif isinstance(geometry, MultiPoint):
        reasons = _multipoint_degeneracy_reasons(geometry, grid_size, where)
    elif isinstance(
        geometry,
        MultiLineString | MultiPolygon | GeometryCollection,
    ):
        parts = cast(
            "list[Geometry | GeometryCollection]",
            list(geometry.geoms),
        )
        for k, part in enumerate(parts):
            reasons.extend(
                _degeneracy_reasons(part, grid_size, f"part {k} of {where}"),
            )
    return reasons


def _as_geometry(
    obj: GeoType | GeoCollectionType | GeoInterface | GeoCollectionInterface,
) -> Geometry | GeometryCollection:
    if isinstance(
        obj,
        Point
        | LineString
        | LinearRing
        | Polygon
        | MultiPoint
        | MultiLineString
        | MultiPolygon
        | GeometryCollection,
    ):
        return obj
    return shape(obj)


def compare_identity(
    first: GeoType | GeoCollectionType | GeoInterface | GeoCollectionInterface,
    second: GeoType | GeoCollectionType | GeoInterface | GeoCollectionInterface,
    *,
    grid_size: float | None = None,
) -> IdentityMatch:
    """
    Compare two geometries for geometric identity, with an audit trail.

    Two geometries are identical when they describe the same shape, even
    if their coordinates were transcribed differently: rings may start at
    any vertex and be traversed in either direction, interior rings
    (holes) and the parts of multi-part geometries may be listed in any
    order.

    Parameters
    ----------
    first, second :
        The geometries to compare; anything implementing
        ``__geo_interface__`` (including all pygeoif geometries) or a
        GeoJSON-like mapping.
    grid_size :
        Optional size of the quantization grid.  When given, coordinates
        are snapped to the nearest grid point before comparing.  If the
        quantization collapses a ring (for example presses a narrow hole
        into a line) or merges distinct points, the verdict is
        ``IdentityVerdict.UNDECIDABLE`` - the information needed to decide
        was destroyed, so equivalence must not be claimed.

    Returns
    -------
    IdentityMatch
        The verdict together with human readable reasons and, for
        identical geometries, the correspondences between the rings and
        vertices of both geometries.  Repeated vertices or repeated rings
        may allow several correspondences; all of them (up to
        ``MAX_CORRESPONDENCES``) are reported.

    Example
    -------
    The same parcel, transcribed from a different starting vertex, with
    the exterior ring reversed and the holes listed in another order:

      >>> from pygeoif import Polygon, compare_identity
      >>> a = Polygon(
      ...     [(0, 0), (4, 0), (4, 4), (0, 4), (0, 0)],
      ...     holes=[
      ...         [(1, 1), (2, 1), (2, 2), (1, 2), (1, 1)],
      ...         [(3, 3), (3.5, 3), (3.5, 3.5), (3, 3)],
      ...     ],
      ... )
      >>> b = Polygon(
      ...     [(4, 4), (4, 0), (0, 0), (0, 4), (4, 4)],
      ...     holes=[
      ...         [(3, 3), (3.5, 3.5), (3.5, 3), (3, 3)],
      ...         [(2, 2), (2, 1), (1, 1), (1, 2), (2, 2)],
      ...     ],
      ... )
      >>> match = compare_identity(a, b)
      >>> match.verdict
      <IdentityVerdict.IDENTICAL: 'identical'>
      >>> print(match.reasons[0])
      Exterior rings coincide (1 vertex alignment(s)).
      >>> print(match.reasons[1])
      Interior ring 0 of the first geometry corresponds to interior ring 1 \
of the second geometry (1 vertex alignment(s)).
      >>> print(match.correspondences[0].rings[1].second_ring)
      ('interior', 1)

    """
    if grid_size is not None and (grid_size <= 0 or not math.isfinite(grid_size)):
        msg = f"grid_size must be a positive finite number, got {grid_size}"
        raise ValueError(msg)
    geometry1 = _as_geometry(first)
    geometry2 = _as_geometry(second)
    if geometry1.geom_type != geometry2.geom_type:
        return IdentityMatch(
            verdict=IdentityVerdict.DIFFERENT,
            grid_size=grid_size,
            correspondences=(),
            reasons=(
                "Geometry types differ: "
                f"{geometry1.geom_type} and {geometry2.geom_type}.",
            ),
        )
    if geometry1.is_empty or geometry2.is_empty:
        if geometry1.is_empty and geometry2.is_empty:
            return IdentityMatch(
                verdict=IdentityVerdict.UNDECIDABLE,
                grid_size=grid_size,
                correspondences=(),
                reasons=(
                    "Both geometries are empty; there are no coordinates "
                    "to compare.",
                ),
            )
        return IdentityMatch(
            verdict=IdentityVerdict.DIFFERENT,
            grid_size=grid_size,
            correspondences=(),
            reasons=("One geometry is empty and the other is not.",),
        )
    if grid_size is not None:
        degeneracy = _degeneracy_reasons(
            geometry1,
            grid_size,
            "the first geometry",
        ) + _degeneracy_reasons(geometry2, grid_size, "the second geometry")
        if degeneracy:
            return IdentityMatch(
                verdict=IdentityVerdict.UNDECIDABLE,
                grid_size=grid_size,
                correspondences=(),
                reasons=tuple(degeneracy),
            )
    result = _compare(geometry1, geometry2, grid_size)
    return IdentityMatch(
        verdict=(
            IdentityVerdict.IDENTICAL if result.identical else IdentityVerdict.DIFFERENT
        ),
        grid_size=grid_size,
        correspondences=result.correspondences if result.identical else (),
        reasons=result.reasons,
        truncated=result.truncated,
    )


__all__ = [
    "MAX_CORRESPONDENCES",
    "Correspondence",
    "IdentityMatch",
    "IdentityVerdict",
    "RingCorrespondence",
    "RingPath",
    "VertexAlignment",
    "compare_identity",
]
