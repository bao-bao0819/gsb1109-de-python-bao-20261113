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
"""Functions for geometries."""

import math
from collections.abc import Iterable
from collections.abc import Sequence
from itertools import groupby
from itertools import pairwise
from itertools import zip_longest
from typing import cast

from pygeoif.types import CoordinatesType
from pygeoif.types import GeoCollectionInterface
from pygeoif.types import GeoInterface
from pygeoif.types import LineType
from pygeoif.types import MultiCoordinatesType
from pygeoif.types import Point2D
from pygeoif.types import PointType


def signed_area(coords: LineType) -> float:
    """
    Return the signed area enclosed by a ring.

    Linear time algorithm: http://www.cgafaq.info/wiki/Polygon_Area.
    A value >= 0 indicates a counter-clockwise oriented ring.
    """
    if len(coords) < 3:  # noqa: PLR2004
        return 0.0
    xs, ys = map(list, zip(*(coord[:2] for coord in coords), strict=True))
    xs.append(xs[1])  # pragma: no mutate
    ys.append(ys[1])  # pragma: no mutate
    return cast(
        "float",
        sum(xs[i] * (ys[i + 1] - ys[i - 1]) for i in range(1, len(coords))) / 2.0,
    )


def centroid(coords: LineType) -> tuple[Point2D, float]:
    """Calculate the coordinates of the centroid and the area of a LineString."""
    ans: list[float] = [0, 0]
    n = len(coords)
    signed_area = 0.0

    # For all vertices
    for i, coord in enumerate(coords):
        next_coord = coords[(i + 1) % n]
        # Calculate area using shoelace formula
        area = (coord[0] * next_coord[1]) - (next_coord[0] * coord[1])
        signed_area += area

        # Calculate coordinates of centroid of polygon
        ans[0] += (coord[0] + next_coord[0]) * area
        ans[1] += (coord[1] + next_coord[1]) * area

    if signed_area == 0 or math.isnan(signed_area):
        return ((math.nan, math.nan), signed_area)

    ans[0] = ans[0] / (3 * signed_area)
    ans[1] = ans[1] / (3 * signed_area)

    return cast("Point2D", tuple(ans)), signed_area / 2.0


def dedupe(coords: LineType) -> LineType:
    """Remove duplicate Points from a LineString."""
    return cast("LineType", tuple(coord for coord, _count in groupby(coords)))


def _point_segment_distance(
    point: PointType,
    start: PointType,
    end: PointType,
) -> float:
    """
    Return the Euclidean distance from ``point`` to the segment ``start``-``end``.

    Only the first two coordinate dimensions are taken into account.
    """
    px, py = point[0], point[1]
    ax, ay = start[0], start[1]
    dx, dy = end[0] - ax, end[1] - ay
    length_squared = dx * dx + dy * dy
    if length_squared == 0.0:
        return math.hypot(px - ax, py - ay)
    projection = ((px - ax) * dx + (py - ay) * dy) / length_squared
    projection = min(1.0, max(0.0, projection))
    closest_x = ax + projection * dx
    closest_y = ay + projection * dy
    return math.hypot(px - closest_x, py - closest_y)


def _locked_indices(
    coordinates: Sequence[PointType],
    preserve: Iterable[PointType],
) -> set[int]:
    """Return the indices of endpoints and coordinates that must be kept."""
    locked = {0, len(coordinates) - 1}
    for landmark in preserve:
        for index, coordinate in enumerate(coordinates):
            if coordinate == landmark:
                locked.add(index)
                break
    return locked


def _farthest_point(
    coordinates: Sequence[PointType],
    first: int,
    last: int,
) -> tuple[float, int]:
    """Find the interior point with the greatest distance to the end chord."""
    max_distance = -1.0
    farthest = -1
    for index in range(first + 1, last):
        distance = _point_segment_distance(
            coordinates[index],
            coordinates[first],
            coordinates[last],
        )
        if distance > max_distance:
            max_distance = distance
            farthest = index
    return max_distance, farthest


def simplify_line(
    coords: LineType,
    tolerance: float,
    preserve: Iterable[PointType] | None = None,
) -> LineType:
    """
    Simplify a LineString using the iterative Douglas-Peucker algorithm.

    Points are removed while every removed point stays within ``tolerance``
    of the resulting line. The first and last coordinate are always kept and
    every coordinate listed in ``preserve`` is retained as well.

    A non-positive ``tolerance`` returns the coordinates unchanged.
    Original coordinate tuples (including any third dimension) are preserved.
    """
    coordinates: tuple[PointType, ...] = tuple(coords)
    if len(coordinates) < 3 or tolerance <= 0.0:  # noqa: PLR2004
        return cast("LineType", coordinates)
    keep = bytearray(len(coordinates))
    locked = _locked_indices(coordinates, preserve or ())
    for index in locked:
        keep[index] = 1
    ordered_locks = sorted(locked)
    stack = list(pairwise(ordered_locks))
    while stack:
        first, last = stack.pop()
        if last - first < 2:  # noqa: PLR2004
            continue
        max_distance, farthest = _farthest_point(coordinates, first, last)
        if max_distance > tolerance:
            keep[farthest] = 1
            stack.append((first, farthest))
            stack.append((farthest, last))
    return cast(
        "LineType",
        tuple(coordinates[index] for index in range(len(coordinates)) if keep[index]),
    )


def _on_segment(point: PointType, start: PointType, end: PointType) -> bool:
    """Check if a collinear ``point`` lies on the segment ``start``-``end``."""
    return min(start[0], end[0]) <= point[0] <= max(start[0], end[0]) and min(
        start[1],
        end[1],
    ) <= point[1] <= max(start[1], end[1])


def _segments_intersect(
    first_start: PointType,
    first_end: PointType,
    second_start: PointType,
    second_end: PointType,
) -> bool:
    """
    Check whether two line segments cross or overlap.

    Collinear overlaps and endpoint touches are reported as intersections.
    """
    orientation_1 = _orientation(first_start, first_end, second_start)
    orientation_2 = _orientation(first_start, first_end, second_end)
    orientation_3 = _orientation(second_start, second_end, first_start)
    orientation_4 = _orientation(second_start, second_end, first_end)
    if orientation_1 == 0.0 and _on_segment(
        second_start,
        first_start,
        first_end,
    ):
        return True
    if orientation_2 == 0.0 and _on_segment(
        second_end,
        first_start,
        first_end,
    ):
        return True
    if orientation_3 == 0.0 and _on_segment(
        first_start,
        second_start,
        second_end,
    ):
        return True
    if orientation_4 == 0.0 and _on_segment(
        first_end,
        second_start,
        second_end,
    ):
        return True
    straddle_first = (
        orientation_1 > 0.0 > orientation_2 or orientation_1 < 0.0 < orientation_2
    )
    straddle_second = (
        orientation_3 > 0.0 > orientation_4 or orientation_3 < 0.0 < orientation_4
    )
    return straddle_first and straddle_second


def is_simple_ring(coords: Sequence[PointType]) -> bool:
    """
    Return True when a LinearRing does not cross or overlap itself.

    The ring may be passed open or closed (last coordinate equal to the first).
    Edges that merely share their consecutive endpoint are not considered
    intersections.
    """
    points: tuple[PointType, ...] = tuple(coords)
    if len(points) > 1 and points[0] == points[-1]:
        points = points[:-1]
    point_count = len(points)
    if point_count < 3:  # noqa: PLR2004
        return True
    for index in range(point_count):
        start: PointType = points[index]
        end: PointType = points[(index + 1) % point_count]
        for other in range(index + 1, point_count):
            are_adjacent = other == index + 1 or (
                index == 0 and other == point_count - 1
            )
            if are_adjacent:
                continue
            other_start: PointType = points[other]
            other_end: PointType = points[(other + 1) % point_count]
            if _segments_intersect(start, end, other_start, other_end):
                return False
    return True


def _orientation(p: PointType, q: PointType, r: PointType) -> float:
    """
    Calculate orientation of three points (p, q, r).

    Returns
    -------
    negative if counterclockwise
    0 if colinear
    positive if clockwise

    """
    return (q[1] - p[1]) * (r[0] - q[0]) - (q[0] - p[0]) * (r[1] - q[1])


def _hull(points: Iterable[Point2D]) -> list[Point2D]:
    """Construct the upper/lower hull of a set of points."""
    stack: list[Point2D] = []
    for p in points:
        while (
            len(stack) >= 2  # noqa: PLR2004
            and _orientation(stack[-2], stack[-1], p) >= 0
        ):
            stack.pop()
        stack.append(p)
    return stack


def convex_hull(points: Iterable[Point2D]) -> LineType:
    """
    Return the convex hull of a set of points using Andrew's monotone chain algorithm.

    Args:
    ----
        points (Iterable[Point2D]): A collection of 2D points.

    Returns:
    -------
        LineType: The convex hull, represented as a list of points.

    """
    points = sorted(set(points))
    # No points, a single point or a line between two points
    if len(points) <= 2:  # noqa: PLR2004
        return points

    # Construct the upper and lower hulls
    upper = _hull(points)
    lower = _hull(reversed(points))
    if len(lower) == len(upper) == 2 and set(lower) == set(upper):  # noqa: PLR2004
        # all points are in a straight line
        return upper
    # Remove duplicate points (at the end of upper and beginning of lower)
    return dedupe(upper + lower)


def compare_coordinates(
    coords: float | CoordinatesType | MultiCoordinatesType,
    other: float | CoordinatesType | MultiCoordinatesType,
) -> bool:
    """Compare two coordinate sequences."""
    try:
        return all(
            compare_coordinates(coords=c, other=o)
            for c, o in zip_longest(
                coords,  # type: ignore [arg-type]
                other,  # type: ignore [arg-type]
                fillvalue=math.nan,
            )
        )
    except TypeError:
        try:
            return math.isclose(a=cast("float", coords), b=cast("float", other))
        except TypeError:
            return False


def compare_geo_interface(
    first: GeoInterface | GeoCollectionInterface,
    second: GeoInterface | GeoCollectionInterface,
) -> bool:
    """Compare two geo interfaces."""
    try:
        if first["type"] != second["type"]:
            return False
        if first["type"] == "GeometryCollection":
            return all(
                compare_geo_interface(first=g1, second=g2)  # type: ignore [arg-type]
                for g1, g2 in zip_longest(
                    first["geometries"],
                    second["geometries"],  # type: ignore [typeddict-item]
                    fillvalue={"type": None, "coordinates": ()},
                )
            )
        return compare_coordinates(
            coords=first["coordinates"],
            other=second["coordinates"],  # type: ignore [typeddict-item]
        )
    except KeyError:
        return False


def move_coordinate(
    coordinate: PointType,
    move_by: PointType,
) -> PointType:
    """
    Move the coordinate by the given vector.

    This forcefully changes the dimensions of the coordinate to match the latter.
    >>> move_coordinate((0, 0), (-1, 1))
    (-1, 1)
    >>> move_coordinate((0, 0, 0), (-1, 1))
    (-1, 1)
    >>> move_coordinate((0, 0), (-1, 1, 0))
    (-1, 1, 0)
    """
    if len(coordinate) < len(move_by):
        return cast(
            "PointType",
            tuple(c + m for c, m in zip_longest(coordinate, move_by, fillvalue=0)),
        )

    return cast(
        "PointType",
        tuple(c + m for c, m in zip(coordinate, move_by, strict=False)),
    )


def move_coordinates(
    coordinates: CoordinatesType,
    move_by: PointType,
) -> CoordinatesType:
    """
    Move the coordinates recursively by the given vector.

    This forcefully changes the dimension of each of the coordinate to match
    the dimensionality of the vector.
    >>> move_coordinates(((0, 0), (-1, 1)), (-1, 1))
    ((-1, 1), (-2, 2))
    >>> move_coordinates(((0, 0, 0), (-1, 1, 0)), (-1, 1))
    ((-1, 1), (-2, 2))
    >>> move_coordinates(((0, 0), (-1, 1)), (-1, 1, 0))
    ((-1, 1, 0), (-2, 2, 0))
    """
    if not coordinates:
        return coordinates
    if isinstance(coordinates[0], (int, float)):
        return move_coordinate(cast("PointType", coordinates), move_by)
    return cast(
        "CoordinatesType",
        tuple(
            move_coordinates(cast("CoordinatesType", c), move_by) for c in coordinates
        ),
    )


def move_geo_interface(
    interface: GeoInterface | GeoCollectionInterface,
    move_by: PointType,
) -> GeoInterface | GeoCollectionInterface:
    """Move the coordinates of the geo interface by the given vector."""
    if interface["type"] == "GeometryCollection":
        return {
            "type": "GeometryCollection",
            "geometries": tuple(
                move_geo_interface(g, move_by) for g in interface["geometries"]
            ),
        }
    return {
        "type": interface["type"],
        "coordinates": move_coordinates(
            interface["coordinates"],  # type: ignore [arg-type]
            move_by,
        ),
    }


__all__ = [
    "centroid",
    "compare_coordinates",
    "compare_geo_interface",
    "convex_hull",
    "dedupe",
    "is_simple_ring",
    "move_coordinate",
    "move_coordinates",
    "signed_area",
    "simplify_line",
]
