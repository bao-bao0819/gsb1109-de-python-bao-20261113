"""
Road connectivity for three dimensional lines.

Road collection systems usually project three dimensional lines to two
dimensions before handing them to a statistics backend.  A planar crossing of
two projected lines is therefore not enough to decide whether a vehicle can
switch between the roads: the elevations at the crossing point matter.

This module keeps both representations:

* the original Z values are interpolated along each line at the planar
  crossing, so a crossing in the middle of a segment uses the elevation at that
  exact position instead of the nearest vertex,
* the exported features are plain two dimensional GeoJSON-like mappings,
  while the accompanying connectivity payload records the elevation
  comparison for every crossing.

Each crossing is classified as ``connected`` (明确可接), ``separated``
(明确分离), or ``review`` (待核查).  The uncertainty band is defined by
``connect_tolerance`` and ``separation_tolerance``.  Elevations whose
difference is at most ``connect_tolerance`` are treated as the same height.
Differences greater than ``separation_tolerance`` are clearly separated.
Everything in between is left for manual review.  When
``separation_tolerance`` is omitted it defaults to twice
``connect_tolerance``, which corresponds to two elevations each carrying the
same symmetric error band.

Lines are normalized to a canonical direction before any parameter, distance,
or node identifier is calculated.  Reversing the input coordinate order of a
line therefore produces the same connections, node identifiers, and reachable
paths.  The exported features drop Z values but also use the canonical
direction, so reversing an input line does not change the export at all.
"""

import math
from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from itertools import pairwise
from typing import Any
from typing import Literal
from typing import cast

from pygeoif.exceptions import DimensionError
from pygeoif.types import LineType
from pygeoif.types import Point2D
from pygeoif.types import Point3D

_POINT_TOLERANCE = 1e-12
_PARAM_TOLERANCE = 1e-12


class ConnectionStatus(str, Enum):
    """Result of comparing two elevations at a planar crossing."""

    CONNECTED = "connected"
    SEPARATED = "separated"
    REVIEW = "review"


@dataclass(frozen=True)
class Intersection:
    """A planar intersection with the elevation on both lines."""

    line_a: str
    line_b: str
    x: float
    y: float
    z_a: float
    z_b: float
    distance_a: float
    distance_b: float
    fraction_a: float
    fraction_b: float
    kind: Literal["crossing", "touch", "overlap"]

    @property
    def xy(self) -> Point2D:
        """Return the planar coordinates of the intersection."""
        return (self.x, self.y)

    @property
    def z_difference(self) -> float:
        """Return the absolute elevation difference."""
        return abs(self.z_a - self.z_b)


@dataclass(frozen=True)
class NodeConnection:
    """Connectivity between the two line nodes that meet at a crossing."""

    line_a: str
    line_b: str
    node_a: str
    node_b: str
    x: float
    y: float
    z_a: float
    z_b: float
    distance_a: float
    distance_b: float
    fraction_a: float
    fraction_b: float
    status: ConnectionStatus
    kind: Literal["crossing", "touch", "overlap"]

    @property
    def xy(self) -> Point2D:
        """Return the planar coordinates of the crossing."""
        return (self.x, self.y)

    @property
    def z_difference(self) -> float:
        """Return the absolute elevation difference."""
        return abs(self.z_a - self.z_b)

    @property
    def is_connected(self) -> bool:
        """Return whether the nodes are clearly connectable."""
        return self.status is ConnectionStatus.CONNECTED

    @property
    def is_separated(self) -> bool:
        """Return whether the nodes are clearly separated."""
        return self.status is ConnectionStatus.SEPARATED

    @property
    def needs_review(self) -> bool:
        """Return whether the crossing needs manual review."""
        return self.status is ConnectionStatus.REVIEW

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON friendly representation."""
        return {
            "line_a": self.line_a,
            "line_b": self.line_b,
            "node_a": self.node_a,
            "node_b": self.node_b,
            "coordinates": [self.x, self.y],
            "z_a": self.z_a,
            "z_b": self.z_b,
            "z_difference": self.z_difference,
            "distance_a": self.distance_a,
            "distance_b": self.distance_b,
            "fraction_a": self.fraction_a,
            "fraction_b": self.fraction_b,
            "status": self.status.value,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class Node:
    """A node in the exported road graph."""

    id: str
    line: str
    kind: Literal["start", "end", "crossing"]
    x: float
    y: float
    z: float
    distance: float
    fraction: float

    @property
    def coordinates(self) -> Point3D:
        """Return the original three dimensional coordinates."""
        return (self.x, self.y, self.z)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON friendly representation."""
        return {
            "id": self.id,
            "line": self.line,
            "kind": self.kind,
            "coordinates": [self.x, self.y, self.z],
            "distance": self.distance,
            "fraction": self.fraction,
        }


@dataclass(frozen=True)
class Edge:
    """An edge in the exported road graph."""

    source: str
    target: str
    kind: Literal["line", "transfer"]
    status: ConnectionStatus
    z_difference: float | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON friendly representation."""
        return {
            "source": self.source,
            "target": self.target,
            "kind": self.kind,
            "status": self.status.value,
            "z_difference": self.z_difference,
        }


@dataclass(frozen=True)
class _LineData:
    """Canonical line used internally."""

    line_id: str
    coords: tuple[Point3D, ...]
    lengths: tuple[float, ...]
    cumulative: tuple[float, ...]
    total_length: float

    def point_at(self, segment: int, fraction: float) -> tuple[float, float, float]:
        """Return the interpolated coordinates at a segment fraction."""
        start = self.coords[segment]
        end = self.coords[segment + 1]
        t = min(max(fraction, 0.0), 1.0)
        return (
            start[0] + (end[0] - start[0]) * t,
            start[1] + (end[1] - start[1]) * t,
            start[2] + (end[2] - start[2]) * t,
        )

    def distance_at(self, segment: int, fraction: float) -> float:
        """Return the two dimensional chainage at a segment fraction."""
        t = min(max(fraction, 0.0), 1.0)
        return self.cumulative[segment] + self.lengths[segment] * t


def _line_coords(line: LineType) -> tuple[Point3D, ...]:
    coords = cast("LineType", getattr(line, "coords", line))
    points: list[Point3D] = []
    for coord in coords:
        if len(coord) < 3:  # noqa: PLR2004
            msg = "Connectivity requires three dimensional coordinates with a Z value"
            raise DimensionError(msg)
        x, y, z = float(coord[0]), float(coord[1]), float(coord[2])
        if not all(math.isfinite(value) for value in (x, y, z)):
            msg = "Connectivity requires finite coordinates"
            raise DimensionError(msg)
        point = (x, y, z)
        if not points or points[-1] != point:
            points.append(point)
    if len(points) < 2:  # noqa: PLR2004
        msg = "A line needs at least two distinct coordinates"
        raise ValueError(msg)
    return tuple(points)


def _prepare_line(line: LineType, line_id: str) -> _LineData:
    coords = _line_coords(line)
    reversed_coords = coords[::-1]
    coords = min(coords, reversed_coords)
    lengths = tuple(
        math.hypot(end[0] - start[0], end[1] - start[1])
        for start, end in pairwise(coords)
    )
    cumulative: tuple[float, ...] = (0.0,)
    for length in lengths:
        cumulative += (cumulative[-1] + length,)
    return _LineData(
        line_id=line_id,
        coords=coords,
        lengths=lengths,
        cumulative=cumulative,
        total_length=cumulative[-1],
    )


def _cross(ax: float, ay: float, bx: float, by: float) -> float:
    return ax * by - ay * bx


def _dot(ax: float, ay: float, bx: float, by: float) -> float:
    return ax * bx + ay * by


def _project(point: Point3D, start: Point3D, end: Point3D) -> tuple[float, float]:
    """Return the projection parameter and squared distance of a point."""
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    denominator = dx * dx + dy * dy
    if denominator <= _POINT_TOLERANCE:
        return 0.0, math.hypot(point[0] - start[0], point[1] - start[1])
    parameter = _dot(point[0] - start[0], point[1] - start[1], dx, dy) / denominator
    px = start[0] + dx * parameter
    py = start[1] + dy * parameter
    return parameter, math.hypot(point[0] - px, point[1] - py)


def _clamp(parameter: float) -> float:
    return min(max(parameter, 0.0), 1.0)


def _within_segment(parameter: float) -> bool:
    return -_PARAM_TOLERANCE <= parameter <= 1.0 + _PARAM_TOLERANCE


def _degenerate_intersections(
    segments: tuple[Point3D, Point3D, Point3D, Point3D],
    lengths: tuple[float, float],
    xy_tolerance: float,
) -> tuple[tuple[float, float, Literal["crossing", "touch", "overlap"]], ...]:
    """Handle segments that collapse to a point in the XY projection."""
    first, second, third, fourth = segments
    first_length, second_length = lengths
    if first_length <= xy_tolerance and second_length <= xy_tolerance:
        if math.hypot(third[0] - first[0], third[1] - first[1]) <= xy_tolerance:
            return ((0.0, 0.0, "touch"),)
        return ()
    if first_length <= xy_tolerance:
        parameter, distance = _project(first, third, fourth)
        if distance <= xy_tolerance and _within_segment(parameter):
            return ((0.0, _clamp(parameter), "touch"),)
        return ()
    parameter, distance = _project(third, first, second)
    if distance <= xy_tolerance and _within_segment(parameter):
        return ((_clamp(parameter), 0.0, "touch"),)
    return ()


def _is_parallel(
    first: Point3D,
    second: Point3D,
    third: Point3D,
    fourth: Point3D,
    xy_tolerance: float,
) -> bool:
    rx = second[0] - first[0]
    ry = second[1] - first[1]
    sx = fourth[0] - third[0]
    sy = fourth[1] - third[1]
    scale = math.hypot(rx, ry) * math.hypot(sx, sy)
    return abs(_cross(rx, ry, sx, sy)) <= xy_tolerance * max(scale, 1.0)


def _proper_intersection(
    first: Point3D,
    second: Point3D,
    third: Point3D,
    fourth: Point3D,
) -> tuple[tuple[float, float, Literal["crossing", "touch", "overlap"]], ...]:
    """Handle segments that cross or touch at a single point."""
    rx = second[0] - first[0]
    ry = second[1] - first[1]
    sx = fourth[0] - third[0]
    sy = fourth[1] - third[1]
    qpx = third[0] - first[0]
    qpy = third[1] - first[1]
    denominator = _cross(rx, ry, sx, sy)
    first_parameter = _cross(qpx, qpy, sx, sy) / denominator
    second_parameter = _cross(qpx, qpy, rx, ry) / denominator
    if not (_within_segment(first_parameter) and _within_segment(second_parameter)):
        return ()
    on_endpoint = any(
        math.isclose(parameter, 0.0, abs_tol=_PARAM_TOLERANCE)
        or math.isclose(parameter, 1.0, abs_tol=_PARAM_TOLERANCE)
        for parameter in (first_parameter, second_parameter)
    )
    return (
        (
            _clamp(first_parameter),
            _clamp(second_parameter),
            "touch" if on_endpoint else "crossing",
        ),
    )


def _collinear_intersections(
    first: Point3D,
    second: Point3D,
    third: Point3D,
    fourth: Point3D,
    xy_tolerance: float,
) -> tuple[tuple[float, float, Literal["crossing", "touch", "overlap"]], ...]:
    """Handle parallel segments that share part of their XY footprint."""
    rx = second[0] - first[0]
    ry = second[1] - first[1]
    sx = fourth[0] - third[0]
    sy = fourth[1] - third[1]
    qpx = third[0] - first[0]
    qpy = third[1] - first[1]
    _, distance = _project(third, first, second)
    if distance > xy_tolerance:
        return ()
    projection = _dot(rx, ry, rx, ry)
    first_start = _dot(qpx, qpy, rx, ry) / projection
    first_end = _dot(sx + qpx, sy + qpy, rx, ry) / projection
    low = max(0.0, min(first_start, first_end))
    high = min(1.0, max(first_start, first_end))
    if high < low - _PARAM_TOLERANCE:
        return ()
    parameters = (
        (low,)
        if math.isclose(
            high,
            low,
            abs_tol=_PARAM_TOLERANCE,
        )
        else (low, high)
    )
    kind: Literal["touch", "overlap"] = "touch" if len(parameters) == 1 else "overlap"
    return tuple(
        (
            parameter,
            _clamp(
                _project(
                    (first[0] + rx * parameter, first[1] + ry * parameter, 0.0),
                    third,
                    fourth,
                )[0],
            ),
            kind,
        )
        for parameter in parameters
    )


def _segment_intersections(
    first: Point3D,
    second: Point3D,
    third: Point3D,
    fourth: Point3D,
    xy_tolerance: float,
) -> tuple[tuple[float, float, Literal["crossing", "touch", "overlap"]], ...]:
    """Return local parameters for all planar intersections of two segments."""
    first_length = math.hypot(second[0] - first[0], second[1] - first[1])
    second_length = math.hypot(fourth[0] - third[0], fourth[1] - third[1])
    if first_length <= xy_tolerance or second_length <= xy_tolerance:
        return _degenerate_intersections(
            (first, second, third, fourth),
            (first_length, second_length),
            xy_tolerance,
        )
    if _is_parallel(first, second, third, fourth, xy_tolerance):
        return _collinear_intersections(first, second, third, fourth, xy_tolerance)
    return _proper_intersection(first, second, third, fourth)


def _raw_intersections(
    first: _LineData,
    second: _LineData,
    xy_tolerance: float,
) -> tuple[Intersection, ...]:
    intersections: list[Intersection] = []
    seen: set[tuple[float, float, float, float]] = set()
    for first_index in range(len(first.coords) - 1):
        for second_index in range(len(second.coords) - 1):
            candidates = _segment_intersections(
                first.coords[first_index],
                first.coords[first_index + 1],
                second.coords[second_index],
                second.coords[second_index + 1],
                xy_tolerance,
            )
            for first_fraction, second_fraction, kind in candidates:
                x, y, z_a = first.point_at(first_index, first_fraction)
                _, _, z_b = second.point_at(second_index, second_fraction)
                distance_a = first.distance_at(first_index, first_fraction)
                distance_b = second.distance_at(second_index, second_fraction)
                key = (
                    round(x, 9),
                    round(y, 9),
                    round(distance_a, 9),
                    round(distance_b, 9),
                )
                if key in seen:
                    continue
                seen.add(key)
                intersections.append(
                    Intersection(
                        line_a=first.line_id,
                        line_b=second.line_id,
                        x=x,
                        y=y,
                        z_a=z_a,
                        z_b=z_b,
                        distance_a=distance_a,
                        distance_b=distance_b,
                        fraction_a=(
                            distance_a / first.total_length
                            if first.total_length > 0
                            else 0.0
                        ),
                        fraction_b=(
                            distance_b / second.total_length
                            if second.total_length > 0
                            else 0.0
                        ),
                        kind=kind,
                    ),
                )
    intersections.sort(key=lambda item: (item.distance_a, item.distance_b))
    return tuple(intersections)


def _validate_tolerances(
    connect_tolerance: float,
    separation_tolerance: float | None,
) -> tuple[float, float]:
    if not math.isfinite(connect_tolerance) or connect_tolerance < 0:
        msg = "connect_tolerance must be a finite non-negative number"
        raise ValueError(msg)
    if separation_tolerance is None:
        separation_tolerance = 2.0 * connect_tolerance
    if not math.isfinite(separation_tolerance) or separation_tolerance < 0:
        msg = "separation_tolerance must be a finite non-negative number"
        raise ValueError(msg)
    if separation_tolerance < connect_tolerance:
        msg = "separation_tolerance must not be smaller than connect_tolerance"
        raise ValueError(msg)
    return connect_tolerance, separation_tolerance


def classify_connection(
    z_a: float,
    z_b: float,
    *,
    connect_tolerance: float = 0.0,
    separation_tolerance: float | None = None,
) -> ConnectionStatus:
    """
    Classify the elevation difference between two crossing nodes.

    A difference within ``connect_tolerance`` is clearly connectable.  A
    difference greater than ``separation_tolerance`` is clearly separated.
    The band in between needs review.

    >>> classify_connection(10.0, 10.4, connect_tolerance=0.5)
    <ConnectionStatus.CONNECTED: 'connected'>
    >>> classify_connection(10.0, 12.0, connect_tolerance=0.5)
    <ConnectionStatus.SEPARATED: 'separated'>
    >>> classify_connection(10.0, 10.7, connect_tolerance=0.5)
    <ConnectionStatus.REVIEW: 'review'>
    """
    connect_tolerance, separation_tolerance = _validate_tolerances(
        connect_tolerance,
        separation_tolerance,
    )
    difference = abs(z_a - z_b)
    if difference <= connect_tolerance:
        return ConnectionStatus.CONNECTED
    if difference > separation_tolerance:
        return ConnectionStatus.SEPARATED
    return ConnectionStatus.REVIEW


def _node_id(
    line: _LineData,
    distance: float,
    x: float,
    y: float,
    xy_tolerance: float,
) -> str:
    if distance <= xy_tolerance:
        return f"{line.line_id}:start"
    if line.total_length - distance <= xy_tolerance:
        return f"{line.line_id}:end"
    return f"{line.line_id}:cross:{distance:.9f}@{x:.9f},{y:.9f}"


def _connections_for_pair(
    first: _LineData,
    second: _LineData,
    *,
    connect_tolerance: float,
    separation_tolerance: float,
    xy_tolerance: float,
) -> tuple[NodeConnection, ...]:
    return tuple(
        NodeConnection(
            line_a=intersection.line_a,
            line_b=intersection.line_b,
            node_a=_node_id(
                first,
                intersection.distance_a,
                intersection.x,
                intersection.y,
                xy_tolerance,
            ),
            node_b=_node_id(
                second,
                intersection.distance_b,
                intersection.x,
                intersection.y,
                xy_tolerance,
            ),
            x=intersection.x,
            y=intersection.y,
            z_a=intersection.z_a,
            z_b=intersection.z_b,
            distance_a=intersection.distance_a,
            distance_b=intersection.distance_b,
            fraction_a=intersection.fraction_a,
            fraction_b=intersection.fraction_b,
            status=classify_connection(
                intersection.z_a,
                intersection.z_b,
                connect_tolerance=connect_tolerance,
                separation_tolerance=separation_tolerance,
            ),
            kind=intersection.kind,
        )
        for intersection in _raw_intersections(first, second, xy_tolerance)
    )


def line_intersections(
    first: LineType,
    second: LineType,
    *,
    line_a: str = "a",
    line_b: str = "b",
    xy_tolerance: float = 1e-9,
) -> tuple[Intersection, ...]:
    """
    Return planar intersections with elevations interpolated on both lines.

    The interpolation is performed separately along each line.  A crossing
    that falls in the middle of a segment therefore gets the Z value at that
    exact position, not the Z value of the nearest vertex.

    >>> first = [(0, 0, 10), (10, 0, 10)]
    >>> second = [(5, -5, 12), (5, 5, 8)]
    >>> intersection = line_intersections(first, second)[0]
    >>> intersection.xy
    (5.0, 0.0)
    >>> intersection.z_a, intersection.z_b
    (10.0, 10.0)
    """
    prepared_first = _prepare_line(first, line_a)
    prepared_second = _prepare_line(second, line_b)
    return _raw_intersections(prepared_first, prepared_second, xy_tolerance)


def node_connectivity(  # noqa: PLR0913
    first: LineType,
    second: LineType,
    *,
    line_a: str = "a",
    line_b: str = "b",
    connect_tolerance: float = 0.0,
    separation_tolerance: float | None = None,
    xy_tolerance: float = 1e-9,
) -> tuple[NodeConnection, ...]:
    """
    Return the node connectivity between two three dimensional lines.

    Elevations are taken at the planar crossings and compared using the
    supplied error band.  Node identifiers and parameters are based on a
    canonical line direction, so reversing either line does not change the
    result.

    >>> first = [(0, 0, 10), (10, 0, 10)]
    >>> second = [(5, -5, 10), (5, 5, 10)]
    >>> node_connectivity(first, second)[0].status.value
    'connected'
    """
    connect_tolerance, separation_tolerance = _validate_tolerances(
        connect_tolerance,
        separation_tolerance,
    )
    return _connections_for_pair(
        _prepare_line(first, line_a),
        _prepare_line(second, line_b),
        connect_tolerance=connect_tolerance,
        separation_tolerance=separation_tolerance,
        xy_tolerance=xy_tolerance,
    )


class ConnectivityExport:
    """
    Two dimensional export plus the node connectivity of the source lines.

    ``features`` contains GeoJSON-like LineString features with the Z values
    projected away.  ``nodes``, ``edges``, and ``connections`` keep the full
    relationship, including the original elevations at every crossing.
    """

    def __init__(
        self,
        features: Sequence[dict[str, Any]],
        nodes: Sequence[Node],
        edges: Sequence[Edge],
        connections: Sequence[NodeConnection],
    ) -> None:
        """Initialize the export."""
        self._features = tuple(features)
        self._nodes = tuple(nodes)
        self._edges = tuple(edges)
        self._connections = tuple(connections)

    @property
    def features(self) -> tuple[dict[str, Any], ...]:
        """Return the two dimensional GeoJSON-like features."""
        return self._features

    @property
    def nodes(self) -> tuple[Node, ...]:
        """Return the graph nodes."""
        return self._nodes

    @property
    def edges(self) -> tuple[Edge, ...]:
        """Return the graph edges."""
        return self._edges

    @property
    def connections(self) -> tuple[NodeConnection, ...]:
        """Return the elevation based node connections."""
        return self._connections

    @property
    def line_ids(self) -> tuple[str, ...]:
        """Return the identifiers of the exported lines."""
        return tuple(
            cast("str", feature["properties"]["id"]) for feature in self._features
        )

    def line_adjacency(
        self,
        *,
        allow_review: bool = False,
    ) -> dict[str, set[str]]:
        """Return the line level adjacency graph."""
        allowed = {ConnectionStatus.CONNECTED}
        if allow_review:
            allowed.add(ConnectionStatus.REVIEW)
        node_lines = {node.id: node.line for node in self._nodes}
        adjacency: dict[str, set[str]] = {line_id: set() for line_id in self.line_ids}
        for edge in self._edges:
            if edge.kind != "transfer" or edge.status not in allowed:
                continue
            source_line = node_lines[edge.source]
            target_line = node_lines[edge.target]
            adjacency[source_line].add(target_line)
            adjacency[target_line].add(source_line)
        return adjacency

    def can_travel(
        self,
        start_line: str,
        end_line: str,
        *,
        allow_review: bool = False,
    ) -> bool:
        """
        Return whether a path exists between two lines.

        Only clearly connectable crossings are traversable by default.
        Pass ``allow_review=True`` to also traverse crossings that need
        manual verification.

        >>> export = export_connectivity(
        ...     {
        ...         "a": [(0, 0, 0), (10, 0, 0)],
        ...         "b": [(5, -5, 0), (5, 5, 0)],
        ...     }
        ... )
        >>> export.can_travel("a", "b")
        True
        """
        adjacency = self.line_adjacency(allow_review=allow_review)
        if start_line not in adjacency or end_line not in adjacency:
            msg = f"Unknown line: {start_line!r} or {end_line!r}"
            raise KeyError(msg)
        pending = [start_line]
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current == end_line:
                return True
            if current in visited:
                continue
            visited.add(current)
            pending.extend(adjacency[current] - visited)
        return False

    @property
    def __geo_interface__(self) -> dict[str, Any]:
        """Return a FeatureCollection with an extra connectivity payload."""
        return {
            "type": "FeatureCollection",
            "features": list(self._features),
            "connectivity": {
                "nodes": [node.as_dict() for node in self._nodes],
                "edges": [edge.as_dict() for edge in self._edges],
                "connections": [
                    connection.as_dict() for connection in self._connections
                ],
            },
        }

    def as_dict(self) -> dict[str, Any]:
        """Return the export as a JSON friendly mapping."""
        return self.__geo_interface__


def _feature(line: _LineData) -> dict[str, Any]:
    return {
        "type": "Feature",
        "properties": {"id": line.line_id},
        "geometry": {
            "type": "LineString",
            "coordinates": [(x, y) for x, y, _ in line.coords],
        },
    }


def _line_nodes(
    line: _LineData,
    connections: Sequence[NodeConnection],
    xy_tolerance: float,
) -> tuple[list[Node], list[Edge]]:
    breakpoints: list[Node] = [
        Node(
            id=f"{line.line_id}:start",
            line=line.line_id,
            kind="start",
            x=line.coords[0][0],
            y=line.coords[0][1],
            z=line.coords[0][2],
            distance=0.0,
            fraction=0.0,
        ),
        Node(
            id=f"{line.line_id}:end",
            line=line.line_id,
            kind="end",
            x=line.coords[-1][0],
            y=line.coords[-1][1],
            z=line.coords[-1][2],
            distance=line.total_length,
            fraction=1.0 if line.total_length > 0 else 0.0,
        ),
    ]
    for connection in connections:
        if connection.line_a == line.line_id:
            distance = connection.distance_a
            x, y, z = connection.x, connection.y, connection.z_a
        elif connection.line_b == line.line_id:
            distance = connection.distance_b
            x, y, z = connection.x, connection.y, connection.z_b
        else:
            continue
        breakpoints.append(
            Node(
                id=_node_id(line, distance, x, y, xy_tolerance),
                line=line.line_id,
                kind="crossing",
                x=x,
                y=y,
                z=z,
                distance=distance,
                fraction=distance / line.total_length if line.total_length > 0 else 0.0,
            ),
        )

    breakpoints.sort(key=lambda node: (node.distance, node.id))
    merged: list[Node] = []
    for node in breakpoints:
        if merged and math.isclose(
            node.distance,
            merged[-1].distance,
            abs_tol=xy_tolerance,
        ):
            replaces_crossing = node.kind in {"start", "end"} and (
                merged[-1].kind != node.kind
            )
            if replaces_crossing:
                merged[-1] = node
            continue
        merged.append(node)
    edges = [
        Edge(
            source=start.id,
            target=end.id,
            kind="line",
            status=ConnectionStatus.CONNECTED,
        )
        for start, end in pairwise(merged)
    ]
    return merged, edges


def _prepare_lines(
    lines: Mapping[str, LineType] | Sequence[LineType],
) -> tuple[_LineData, ...]:
    if isinstance(lines, Mapping):
        items: Iterable[tuple[str, LineType]] = lines.items()
    else:
        items = ((f"line-{index}", line) for index, line in enumerate(lines))
    prepared = tuple(_prepare_line(line, str(line_id)) for line_id, line in items)
    if not prepared:
        msg = "At least one line is required"
        raise ValueError(msg)
    return prepared


def _resolve_node_id(
    distance: float,
    fallback: str,
    node_ids: Mapping[float, str],
    xy_tolerance: float,
) -> str:
    """Return the graph node id for a crossing, honoring start/end snapping."""
    for candidate, node_id in node_ids.items():
        if math.isclose(candidate, distance, abs_tol=xy_tolerance):
            return node_id
    return fallback


def export_connectivity(
    lines: Mapping[str, LineType] | Sequence[LineType],
    *,
    connect_tolerance: float = 0.0,
    separation_tolerance: float | None = None,
    xy_tolerance: float = 1e-9,
) -> ConnectivityExport:
    """
    Export two dimensional lines together with their node connectivity.

    Parameters
    ----------
    lines : mapping or sequence
        Three dimensional lines.  A mapping supplies stable line identifiers;
        a sequence gets ``line-0``, ``line-1``, and so on.
    connect_tolerance : float
        Maximum elevation difference that is clearly connectable.
    separation_tolerance : float, optional
        Minimum elevation difference that is clearly separated.  Defaults to
        twice ``connect_tolerance``.
    xy_tolerance : float
        Planar tolerance used for intersection and node matching.

    Returns
    -------
    ConnectivityExport
        Two dimensional features and the node/edge connectivity payload.

    >>> export = export_connectivity(
    ...     {
    ...         "a": [(0, 0, 10), (10, 0, 10)],
    ...         "b": [(5, -5, 12), (5, 5, 8)],
    ...     },
    ...     connect_tolerance=0.5,
    ... )
    >>> export.features[0]["geometry"]["coordinates"][0]
    (0.0, 0.0)
    >>> export.connections[0].status.value
    'connected'

    """
    connect_tolerance, separation_tolerance = _validate_tolerances(
        connect_tolerance,
        separation_tolerance,
    )
    prepared = _prepare_lines(lines)
    connections: list[NodeConnection] = []
    for first_index, first in enumerate(prepared):
        for second in prepared[first_index + 1 :]:
            connections.extend(
                _connections_for_pair(
                    first,
                    second,
                    connect_tolerance=connect_tolerance,
                    separation_tolerance=separation_tolerance,
                    xy_tolerance=xy_tolerance,
                ),
            )
    connections.sort(
        key=lambda item: (item.line_a, item.line_b, item.distance_a, item.distance_b),
    )

    nodes: list[Node] = []
    edges: list[Edge] = []
    node_ids: dict[str, dict[float, str]] = {}
    for line in prepared:
        line_nodes, line_edges = _line_nodes(line, connections, xy_tolerance)
        nodes.extend(line_nodes)
        edges.extend(line_edges)
        node_ids[line.line_id] = {
            node.distance: node.id for node in line_nodes if node.kind == "crossing"
        }

    def transfer_source(connection: NodeConnection) -> str:
        nodes_for_line = {
            **node_ids.get(connection.line_a, {}),
        }
        return _resolve_node_id(
            connection.distance_a,
            connection.node_a,
            nodes_for_line,
            xy_tolerance,
        )

    def transfer_target(connection: NodeConnection) -> str:
        nodes_for_line = {
            **node_ids.get(connection.line_b, {}),
        }
        return _resolve_node_id(
            connection.distance_b,
            connection.node_b,
            nodes_for_line,
            xy_tolerance,
        )

    edges.extend(
        Edge(
            source=transfer_source(connection),
            target=transfer_target(connection),
            kind="transfer",
            status=connection.status,
            z_difference=connection.z_difference,
        )
        for connection in connections
    )
    return ConnectivityExport(
        features=[_feature(line) for line in prepared],
        nodes=nodes,
        edges=edges,
        connections=connections,
    )


__all__ = [
    "ConnectionStatus",
    "ConnectivityExport",
    "Edge",
    "Intersection",
    "Node",
    "NodeConnection",
    "classify_connection",
    "export_connectivity",
    "line_intersections",
    "node_connectivity",
]
