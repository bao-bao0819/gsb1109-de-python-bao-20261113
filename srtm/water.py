# -*- coding: utf-8 -*-

"""
Water level segments along a polyline.

SRTM elevations between four neighbouring grid nodes are bilinear.  Along one
straight piece of a polyline the two local grid coordinates are linear in the
distance travelled, therefore the interpolated elevation is a quadratic in
that distance.  This module solves that quadratic exactly instead of sampling
the line at fixed intervals: a line whose two ends are above the water can
still dip below it in between, and a fixed-step sampler would miss it.

Coordinate order is ``(latitude, longitude)``, the same as the rest of the
library.  Elevations are in the units of the SRTM data (metres).
"""

import math as mod_math

from dataclasses import dataclass
from typing import *

from . import utils as mod_utils


ABOVE = 'above'
BELOW = 'below'
CONTOUR = 'contour'
MISSING = 'missing'

UP = 'up'
DOWN = 'down'

START = 'start'
END = 'end'
INTERSECTION = 'intersection'
NO_DATA = 'no_data'

Point = Tuple[float, float]

_DEFAULT_TOLERANCE = 1e-9
_PARAMETER_TOLERANCE = 1e-12
_MINIMUM_LENGTH = 1e-6


@dataclass(frozen=True)
class WaterCrossing:
    """
    A point where the polyline crosses the requested water level.

    ``direction`` is the elevation trend while travelling along the polyline:
    ``'up'`` when the line leaves the water and ``'down'`` when it enters it.
    Walking the same route backwards swaps the two directions at the
    corresponding (mirrored) positions.
    """

    point: Point
    distance: float
    direction: str
    incoming: str
    outgoing: str

    @property
    def latitude(self) -> float:
        return self.point[0]

    @property
    def longitude(self) -> float:
        return self.point[1]

    @property
    def entering_water(self) -> bool:
        return self.direction == DOWN

    @property
    def leaving_water(self) -> bool:
        return self.direction == UP

    @property
    def is_entering(self) -> bool:
        return self.direction == DOWN

    @property
    def is_leaving(self) -> bool:
        return self.direction == UP

    @property
    def entering(self) -> bool:
        return self.direction == DOWN

    @property
    def leaving(self) -> bool:
        return self.direction == UP

    @property
    def into_water(self) -> bool:
        return self.direction == DOWN

    @property
    def out_of_water(self) -> bool:
        return self.direction == UP

    @property
    def kind(self) -> str:
        return self.direction

    def as_tuple(self) -> Point:
        return self.point

    def __iter__(self) -> Iterator[Any]:
        yield self.point[0]
        yield self.point[1]
        yield self.distance
        yield self.direction


@dataclass(frozen=True)
class WaterLevelSegment:
    """
    A section of the polyline above the water level or exactly on it.

    ``kind`` is ``'above'`` for a section strictly above the water and
    ``'contour'`` for a section which runs along the water line.  The
    ``start_edge``/``end_edge`` fields tell how the section ends: at the
    beginning/end of the polyline (``'start'``/``'end'``), at a real water
    intersection (``'intersection'``), next to a contour section
    (``'contour'``) or next to missing data (``'no_data'``).

    Missing data never produces a fake water intersection; it is reported
    separately, next to the intersections which really exist on each side.
    """

    start: Point
    end: Point
    length: float
    kind: str
    start_distance: float
    end_distance: float
    start_edge: str = INTERSECTION
    end_edge: str = INTERSECTION
    start_direction: Optional[str] = None
    end_direction: Optional[str] = None

    @property
    def latitude(self) -> float:
        return self.start[0]

    @property
    def longitude(self) -> float:
        return self.start[1]

    @property
    def start_latitude(self) -> float:
        return self.start[0]

    @property
    def start_longitude(self) -> float:
        return self.start[1]

    @property
    def end_latitude(self) -> float:
        return self.end[0]

    @property
    def end_longitude(self) -> float:
        return self.end[1]

    @property
    def type(self) -> str:
        return self.kind

    @property
    def segment_type(self) -> str:
        return self.kind

    @property
    def is_above_water(self) -> bool:
        return self.kind == ABOVE

    @property
    def is_contour(self) -> bool:
        return self.kind == CONTOUR

    @property
    def is_level(self) -> bool:
        return self.kind == CONTOUR

    @property
    def points(self) -> Tuple[Point, Point]:
        return self.start, self.end

    @property
    def start_point(self) -> Point:
        return self.start

    @property
    def end_point(self) -> Point:
        return self.end

    @property
    def entry_direction(self) -> Optional[str]:
        return self.start_direction

    @property
    def exit_direction(self) -> Optional[str]:
        return self.end_direction

    @property
    def enter_direction(self) -> Optional[str]:
        return self.start_direction

    @property
    def leave_direction(self) -> Optional[str]:
        return self.end_direction

    @property
    def start_type(self) -> str:
        return self.start_edge

    @property
    def end_type(self) -> str:
        return self.end_edge

    @property
    def start_kind(self) -> str:
        return self.start_edge

    @property
    def end_kind(self) -> str:
        return self.end_edge

    @property
    def direction(self) -> Tuple[Optional[str], Optional[str]]:
        return self.start_direction, self.end_direction

    @property
    def edges(self) -> Tuple[str, str]:
        return self.start_edge, self.end_edge

    def as_tuple(self) -> Tuple[Point, Point, float]:
        return self.start, self.end, self.length

    def as_dict(self) -> Dict[str, Any]:
        return {
            'start': self.start,
            'end': self.end,
            'length': self.length,
            'kind': self.kind,
            'type': self.kind,
            'start_distance': self.start_distance,
            'end_distance': self.end_distance,
            'start_edge': self.start_edge,
            'end_edge': self.end_edge,
            'entry_direction': self.entry_direction,
            'exit_direction': self.exit_direction,
        }

    def get(self, key: str, default: Any = None) -> Any:
        return self.as_dict().get(key, default)

    def __iter__(self) -> Iterator[Any]:
        yield self.start
        yield self.end
        yield self.length

    def __len__(self) -> int:
        return 3

    def __getitem__(self, item: Any) -> Any:
        if isinstance(item, str):
            return getattr(self, item)
        return self.as_tuple()[item]


@dataclass(frozen=True)
class MissingSegment:
    """A stretch of the polyline where bilinear elevation is not available."""

    start: Point
    end: Point
    length: float
    start_distance: float
    end_distance: float

    @property
    def kind(self) -> str:
        return MISSING

    @property
    def type(self) -> str:
        return MISSING

    @property
    def points(self) -> Tuple[Point, Point]:
        return self.start, self.end

    @property
    def is_missing(self) -> bool:
        return True

    def as_tuple(self) -> Tuple[Point, Point, float]:
        return self.start, self.end, self.length

    def __iter__(self) -> Iterator[Any]:
        yield self.start
        yield self.end
        yield self.length

    def __len__(self) -> int:
        return 3


class WaterLevelSegments(list):
    """
    The list of above-water and contour sections, in polyline order.

    Missing-data stretches are exposed separately (``missing``/``gaps``) so
    that a hole in the elevation data is never mistaken for a water
    intersection.  ``crossings`` contains only real intersections with the
    requested water level.  ``intervals`` keeps every piece of the classified
    polyline, including the below-water and missing ones.
    """

    def __init__(self, segments: Sequence[WaterLevelSegment],
                 missing: Sequence[MissingSegment],
                 crossings: Sequence[WaterCrossing],
                 intervals: Sequence[Any],
                 water_level: float,
                 total_distance: float = 0.) -> None:
        super().__init__(segments)
        self.segments = self
        self.above = [segment for segment in segments if segment.kind == ABOVE]
        self.above_water = self.above
        self.above_water_segments = self.above
        self.contours = [segment for segment in segments if segment.kind == CONTOUR]
        self.contour_segments = self.contours
        self.level_segments = self.contours
        self.missing = list(missing)
        self.missing_segments = self.missing
        self.gaps = self.missing
        self.crossings = list(crossings)
        self.intersections = self.crossings
        self.water_crossings = self.crossings
        self.intervals = list(intervals)
        self.all_segments = self.intervals
        self.water_level = water_level
        self.total_distance = total_distance

    @property
    def total_length(self) -> float:
        return sum(segment.length for segment in self)

    @property
    def length(self) -> float:
        return self.total_length

    def as_tuples(self) -> List[Tuple[Point, Point]]:
        return [(segment.start, segment.end) for segment in self]

    def as_dicts(self) -> List[Dict[str, Any]]:
        return [segment.as_dict() for segment in self]

    def total_above_length(self) -> float:
        return sum(segment.length for segment in self.above)

    def total_contour_length(self) -> float:
        return sum(segment.length for segment in self.contours)

    def total_missing_length(self) -> float:
        return sum(segment.length for segment in self.missing)


@dataclass
class _Piece:
    start_index: int
    end_index: int
    start_parameter: float
    end_parameter: float
    kind: str
    length: float
    start: Point
    end: Point
    start_distance: float
    end_distance: float


@dataclass
class _PolylineSegment:
    index: int
    start: Point
    end: Point
    start_distance: float
    length: float


@dataclass
class _Cell:
    row: int
    column: int
    z00: float
    z10: float
    z01: float
    z11: float
    coordinates: Callable[[Point], Tuple[float, float]]

    def value(self, u: float, v: float) -> float:
        return (self.z00
                + (self.z10 - self.z00) * u
                + (self.z01 - self.z00) * v
                + (self.z00 - self.z10 - self.z01 + self.z11) * u * v)


def _as_point(point: Any) -> Point:
    if hasattr(point, 'latitude') and hasattr(point, 'longitude'):
        return float(point.latitude), float(point.longitude)
    if isinstance(point, dict):
        if 'latitude' in point and 'longitude' in point:
            return float(point['latitude']), float(point['longitude'])
        if 'lat' in point and 'lon' in point:
            return float(point['lat']), float(point['lon'])
    return float(point[0]), float(point[1])


def _normalize_points(points: Any) -> List[Point]:
    """Accept a point list, a GPX object, a track or a segment."""
    if hasattr(points, 'tracks'):
        result: List[Point] = []
        for track in points.tracks:
            for segment in track.segments:
                result.extend(_as_point(point) for point in segment.points)
        return result
    if hasattr(points, 'segments'):
        result = []
        for segment in points.segments:
            result.extend(_as_point(point) for point in segment.points)
        return result
    if hasattr(points, 'points'):
        points = points.points
    return [_as_point(point) for point in points]


def _distance(start: Point, end: Point) -> float:
    """
    A symmetric horizontal distance.

    ``mod_utils.distance`` scales the longitude by the latitude of its first
    argument, which would make a section change length when the route is
    walked in the opposite direction.  The mean latitude keeps the same
    result in both directions.
    """
    latitude = (start[0] + end[0]) / 2.
    coef = mod_math.cos(latitude / 180. * mod_math.pi)
    x = start[0] - end[0]
    y = (start[1] - end[1]) * coef
    return mod_math.sqrt(x * x + y * y) * mod_utils.ONE_DEGREE


def _lerp(start: Point, end: Point, parameter: float) -> Point:
    return (start[0] + (end[0] - start[0]) * parameter,
            start[1] + (end[1] - start[1]) * parameter)


def _add_break(breaks: List[float], value: float) -> None:
    if 0. < value < 1.:
        breaks.append(value)


def _integer_breaks(start: float, end: float) -> List[float]:
    """Parameters where a linear value crosses an integer boundary."""
    if start == end:
        return []
    low, high = sorted((start, end))
    result: List[float] = []
    for boundary in range(int(mod_math.floor(low)) + 1, int(mod_math.ceil(high))):
        if low < boundary < high:
            result.append((boundary - start) / (end - start))
    return result


def _quadratic_roots(a: float, b: float, c: float, tolerance: float,
                     scale: float = 1.) -> List[float]:
    """Real roots of ``a*x*x + b*x + c``, in ascending order."""
    if abs(a) <= tolerance:
        if abs(b) <= tolerance:
            return []
        return [-c / b]

    discriminant = b * b - 4. * a * c
    discriminant_tolerance = tolerance * max(scale, 1.)
    if discriminant <= discriminant_tolerance:
        # No sign change: either no crossing or a single tangential touch.
        # A tangential touch does not split an above-water section, and
        # treating it symmetrically keeps reversed routes identical.
        return []

    root = mod_math.sqrt(discriminant)
    if b >= 0.:
        q = -0.5 * (b + root)
    else:
        q = -0.5 * (b - root)

    result = []
    if abs(q) > tolerance:
        result.append(q / a)
        result.append(c / q)
    else:
        result.append(-b / (2. * a))
    result.sort()
    return result


def _unique(values: Sequence[float],
            tolerance: float = _PARAMETER_TOLERANCE) -> List[float]:
    result: List[float] = []
    for value in sorted(values):
        if not result or abs(value - result[-1]) > tolerance:
            result.append(value)
    return result


class _CellAccessor:
    """
    Returns the four corner elevations of the SRTM cell containing a point.

    ``None`` nodes (SRTM voids) make the whole cell unavailable.  No
    extrapolation outside the cell is attempted.
    """

    def __init__(self, elevation_data: Any, cell_size: Optional[float] = None,
                 elevation_function: Optional[Callable[[float, float], Optional[float]]] = None,
                 node_function: Optional[Callable[[float, float], Optional[float]]] = None) -> None:
        self.elevation_data = elevation_data
        self.cell_size = cell_size
        self.elevation_function = elevation_function
        self.node_function = node_function
        self._tile_cache: Dict[Any, Any] = {}
        self._node_cache: Dict[Any, Optional[float]] = {}

    def tile_for(self, latitude: float, longitude: float) -> Any:
        if not hasattr(self.elevation_data, 'get_file'):
            return None
        key = (mod_math.floor(latitude), mod_math.floor(longitude))
        if key not in self._tile_cache:
            try:
                self._tile_cache[key] = self.elevation_data.get_file(latitude, longitude)
            except Exception:
                self._tile_cache[key] = None
        return self._tile_cache[key]

    def _node(self, tile: Any, row: int, column: int) -> Optional[float]:
        key = (getattr(tile, 'file_name', id(tile)), row, column)
        if key not in self._node_cache:
            try:
                self._node_cache[key] = tile.get_elevation_from_row_and_column(row, column)
            except Exception:
                self._node_cache[key] = None
        return self._node_cache[key]

    def _tile_grid(self, tile: Any, latitude: float,
                   longitude: float) -> Optional[Tuple[float, float]]:
        square_side = int(tile.square_side)
        if square_side < 2:
            return None
        side = float(square_side - 1)
        row_value = (tile.latitude + 1. - latitude) * side
        column_value = (longitude - tile.longitude) * side
        if row_value < -1e-7 or row_value > side + 1e-7:
            return None
        if column_value < -1e-7 or column_value > side + 1e-7:
            return None
        return min(max(row_value, 0.), side), min(max(column_value, 0.), side)

    def grid_coordinates(self, point: Point) -> Optional[Tuple[float, float]]:
        """
        Continuous grid coordinates of a point.

        The integer part identifies the cell and the fractional part is the
        position inside it.  This stays valid even when the cell is missing,
        so the line can be split at every grid line before cells are looked
        up.
        """
        tile = self.tile_for(point[0], point[1])
        if tile is not None and hasattr(tile, 'square_side'):
            return self._tile_grid(tile, point[0], point[1])
        if self.cell_size:
            size = float(self.cell_size)
            return point[0] / size, point[1] / size
        return None

    def cell(self, latitude: float, longitude: float) -> Optional[_Cell]:
        tile = self.tile_for(latitude, longitude)
        if tile is not None and hasattr(tile, 'square_side'):
            grid = self._tile_grid(tile, latitude, longitude)
            if grid is None:
                return None
            row_value, column_value = grid
            side = int(tile.square_side - 1)
            row = min(max(int(mod_math.floor(row_value)), 0), side - 1)
            column = min(max(int(mod_math.floor(column_value)), 0), side - 1)
            z00 = self._node(tile, row, column)
            z10 = self._node(tile, row + 1, column)
            z01 = self._node(tile, row, column + 1)
            z11 = self._node(tile, row + 1, column + 1)
            if any(value is None for value in (z00, z10, z01, z11)):
                return None

            def coordinates(point: Point) -> Tuple[float, float]:
                local = self._tile_grid(tile, point[0], point[1])
                if local is None:
                    return 0., 0.
                return local[0] - row, local[1] - column

            return _Cell(row, column, z00, z10, z01, z11, coordinates)  # type: ignore

        if not self.cell_size:
            return None

        if self.elevation_function is None:
            if not hasattr(self.elevation_data, 'get_elevation'):
                return None

            def elevation_function(lat: float, lon: float) -> Optional[float]:
                try:
                    return self.elevation_data.get_elevation(lat, lon, approximate=False)
                except TypeError:
                    return self.elevation_data.get_elevation(lat, lon)
            self.elevation_function = elevation_function

        size = float(self.cell_size)
        row_value = latitude / size
        column_value = longitude / size
        row = int(mod_math.floor(row_value))
        column = int(mod_math.floor(column_value))
        north = (row + 1) * size
        south = row * size
        west = column * size
        east = (column + 1) * size
        # Evaluate the corners slightly inside the cell so that a source
        # which internally floors its coordinates does not select a
        # neighbouring cell when asked for a node.
        epsilon = size * 1e-9
        elevations = [
            self.elevation_function(north - epsilon, west + epsilon),
            self.elevation_function(south + epsilon, west + epsilon),
            self.elevation_function(north - epsilon, east - epsilon),
            self.elevation_function(south + epsilon, east - epsilon),
        ]
        if any(value is None for value in elevations):
            return None
        z00, z10, z01, z11 = elevations

        def coordinates(point: Point) -> Tuple[float, float]:
            return (north - point[0]) / size, (point[1] - west) / size

        return _Cell(row, column, z00, z10, z01, z11, coordinates)  # type: ignore


def _pieces_for_segment(segment: _PolylineSegment, accessor: _CellAccessor,
                        water_level: float, tolerance: float) -> List[_Piece]:
    """Split one straight segment into same-side/contour/missing pieces."""
    start, end = segment.start, segment.end

    breaks = [0., 1.]
    for parameter in _integer_breaks(start[0], end[0]):
        _add_break(breaks, parameter)
    for parameter in _integer_breaks(start[1], end[1]):
        _add_break(breaks, parameter)

    # A tile boundary is an integer degree, so it is already a break here.
    coarse = _unique(breaks)
    result: List[_Piece] = []

    for coarse_index in range(len(coarse) - 1):
        coarse_start = coarse[coarse_index]
        coarse_end = coarse[coarse_index + 1]
        if coarse_end - coarse_start <= _PARAMETER_TOLERANCE:
            continue

        # Grid coordinates are available even inside a missing cell, so the
        # line is first split at every grid line and only then each cell is
        # classified.  Otherwise one void node would hide its whole tile.
        grid_start = accessor.grid_coordinates(_lerp(start, end, coarse_start))
        grid_end = accessor.grid_coordinates(_lerp(start, end, coarse_end))

        cell_breaks = [coarse_start, coarse_end]
        if grid_start is not None and grid_end is not None:
            for parameter in _integer_breaks(grid_start[0], grid_end[0]):
                _add_break(cell_breaks, coarse_start + parameter * (coarse_end - coarse_start))
            for parameter in _integer_breaks(grid_start[1], grid_end[1]):
                _add_break(cell_breaks, coarse_start + parameter * (coarse_end - coarse_start))

        cell_breaks = _unique(cell_breaks)
        for index in range(len(cell_breaks) - 1):
            piece_start = cell_breaks[index]
            piece_end = cell_breaks[index + 1]
            if piece_end - piece_start <= _PARAMETER_TOLERANCE:
                continue

            middle = (piece_start + piece_end) / 2.
            piece_cell = accessor.cell(*_lerp(start, end, middle))
            if piece_cell is None:
                result.append(_make_piece(segment, piece_start, piece_end, MISSING))
                continue

            piece_u0, piece_v0 = piece_cell.coordinates(_lerp(start, end, piece_start))
            piece_u1, piece_v1 = piece_cell.coordinates(_lerp(start, end, piece_end))
            piece_u0 = min(max(piece_u0, 0.), 1.)
            piece_v0 = min(max(piece_v0, 0.), 1.)
            piece_u1 = min(max(piece_u1, 0.), 1.)
            piece_v1 = min(max(piece_v1, 0.), 1.)

            result.extend(_pieces_by_elevation(
                segment, piece_start, piece_end, water_level, tolerance,
                piece_cell.z00, piece_cell.z10, piece_cell.z01, piece_cell.z11,
                piece_u0, piece_v0, piece_u1, piece_v1))

    return result


def _pieces_by_elevation(segment: _PolylineSegment, start_parameter: float,
                         end_parameter: float, water_level: float,
                         tolerance: float, z00: float, z10: float, z01: float,
                         z11: float, u0: float, v0: float, u1: float,
                         v1: float) -> List[_Piece]:
    """Classify one cell interval by solving its quadratic exactly."""
    duration = end_parameter - start_parameter
    if duration <= _PARAMETER_TOLERANCE:
        return []

    du = u1 - u0
    dv = v1 - v0
    a_u = z10 - z00
    a_v = z01 - z00
    a_uv = z00 - z10 - z01 + z11

    # z(t) = a*t^2 + b*t + c with t measured from the start of the interval
    a = a_uv * du * dv
    b = a_u * du + a_v * dv + a_uv * (u0 * dv + v0 * du)
    c = z00 + a_u * u0 + a_v * v0 + a_uv * u0 * v0 - water_level

    scale = max(1., abs(water_level), abs(z00), abs(z10), abs(z01), abs(z11))
    local_tolerance = tolerance * scale

    if abs(a) <= local_tolerance and abs(b) <= local_tolerance:
        kind = CONTOUR if abs(c) <= local_tolerance else (ABOVE if c > 0. else BELOW)
        return [_make_piece(segment, start_parameter, end_parameter, kind)]

    roots = _quadratic_roots(a, b, c, local_tolerance, scale)
    inside = [root for root in roots
              if -_PARAMETER_TOLERANCE <= root <= 1. + _PARAMETER_TOLERANCE]
    points = _unique([0.] + [min(max(root, 0.), 1.) for root in inside] + [1.])

    result: List[_Piece] = []
    for index in range(len(points) - 1):
        local_start = points[index]
        local_end = points[index + 1]
        if local_end - local_start <= _PARAMETER_TOLERANCE:
            continue
        middle = (local_start + local_end) / 2.
        values = [a * parameter * parameter + b * parameter + c
                  for parameter in (local_start, middle, local_end)]
        significant = [value for value in values
                       if abs(value) > local_tolerance]
        if significant:
            kind = ABOVE if significant[0] > 0. else BELOW
        else:
            kind = CONTOUR
        result.append(_make_piece(segment,
                                  start_parameter + local_start * duration,
                                  start_parameter + local_end * duration,
                                  kind))
    return result


def _make_piece(segment: _PolylineSegment, start_parameter: float,
                end_parameter: float, kind: str) -> _Piece:
    start = _lerp(segment.start, segment.end, start_parameter)
    end = _lerp(segment.start, segment.end, end_parameter)
    length = segment.length * (end_parameter - start_parameter)
    return _Piece(segment.index, segment.index,
                  start_parameter, end_parameter, kind, length,
                  start, end,
                  segment.start_distance + segment.length * start_parameter,
                  segment.start_distance + segment.length * end_parameter)


def _merge_pieces(pieces: Sequence[_Piece]) -> List[_Piece]:
    """Join adjacent pieces of the same kind, including across polyline vertices."""
    result: List[_Piece] = []
    for piece in pieces:
        if piece.length <= _MINIMUM_LENGTH:
            continue
        if result and result[-1].kind == piece.kind \
                and abs(result[-1].end_distance - piece.start_distance) <= 1e-9:
            previous = result[-1]
            result[-1] = _Piece(previous.start_index, piece.end_index,
                                previous.start_parameter, piece.end_parameter,
                                previous.kind, previous.length + piece.length,
                                previous.start, piece.end,
                                previous.start_distance, piece.end_distance)
        else:
            result.append(piece)
    return result


def _side(kind: str) -> int:
    if kind == BELOW:
        return -1
    if kind in (ABOVE, CONTOUR):
        return 1
    return 0


def _crossing_direction(incoming: str, outgoing: str) -> Optional[str]:
    if _side(incoming) < 0 <= _side(outgoing):
        return UP
    if _side(incoming) >= 0 > _side(outgoing):
        return DOWN
    return None


def _edge_type(current: str, neighbour: Optional[str],
               is_start: bool) -> str:
    if neighbour is None:
        return START if is_start else END
    if neighbour == MISSING:
        return NO_DATA
    if _side(neighbour) != _side(current):
        return INTERSECTION
    if current == CONTOUR or neighbour == CONTOUR:
        return CONTOUR
    # A grid cell boundary inside one continuous above-water section.
    return 'cell'


def _to_segment(piece: _Piece, previous: Optional[_Piece],
                following: Optional[_Piece],
                crossings: Sequence[WaterCrossing]) -> WaterLevelSegment:
    start_edge = _edge_type(piece.kind,
                            previous.kind if previous is not None else None,
                            True)
    end_edge = _edge_type(piece.kind,
                          following.kind if following is not None else None,
                          False)

    start_direction = None
    end_direction = None
    for crossing in crossings:
        if abs(crossing.distance - piece.start_distance) <= 1e-9:
            start_direction = crossing.direction
        if abs(crossing.distance - piece.end_distance) <= 1e-9:
            end_direction = crossing.direction

    return WaterLevelSegment(
        start=piece.start,
        end=piece.end,
        length=piece.length,
        kind=piece.kind,
        start_distance=piece.start_distance,
        end_distance=piece.end_distance,
        start_edge=start_edge,
        end_edge=end_edge,
        start_direction=start_direction,
        end_direction=end_direction,
    )


def bilinear_elevation(elevation_data: Any, latitude: float, longitude: float,
                       cell_size: Optional[float] = None,
                       elevation_function: Optional[Callable[[float, float], Optional[float]]] = None) -> Optional[float]:
    """Bilinear elevation of one point, or ``None`` when a cell node is missing."""
    accessor = _CellAccessor(elevation_data, cell_size=cell_size,
                             elevation_function=elevation_function)
    cell = accessor.cell(latitude, longitude)
    if cell is None:
        return None
    u, v = cell.coordinates((latitude, longitude))
    return cell.value(u, v)


def get_water_level_segments(elevation_data: Any, points: Any,
                             water_level: float,
                             cell_size: Optional[float] = None,
                             elevation_function: Optional[Callable[[float, float], Optional[float]]] = None,
                             tolerance: float = _DEFAULT_TOLERANCE,
                             include_contours: bool = True,
                             include_below: bool = False) -> WaterLevelSegments:
    """
    Return the polyline sections at or above ``water_level``.

    Elevation is the bilinear interpolation of the four SRTM cell nodes
    around each point, so along a straight piece of the polyline it is a
    quadratic; every water-level crossing is solved analytically.  The result
    contains above-water sections and (unless ``include_contours`` is false)
    sections exactly on the water line, which keep their ``'contour'`` kind.

    Missing cells are reported separately (``.missing``) and are never turned
    into intersections.  ``.crossings`` holds only the real intersections
    with the requested water level.  Sections ending next to missing data
    have ``start_edge``/``end_edge`` set to ``'no_data'``.

    ``elevation_data`` is normally a :class:`srtm.data.GeoElevationData`.  A
    callable may be passed instead (with ``cell_size``), and it may also be
    omitted when ``elevation_function`` is given.  With ``include_below`` the
    below-water sections are returned as well.
    """
    if callable(elevation_data) and not hasattr(elevation_data, 'get_file') \
            and not hasattr(elevation_data, 'get_elevation'):
        if elevation_function is None:
            elevation_function = elevation_data
        elevation_data = None

    normalized_points = _normalize_points(points)
    if len(normalized_points) < 2:
        return WaterLevelSegments([], [], [], [], float(water_level))

    # Repeated points add no geometry and would only introduce zero-length
    # pieces, so a route and its reversal stay identical.
    filtered = [normalized_points[0]]
    for point in normalized_points[1:]:
        if point != filtered[-1]:
            filtered.append(point)
    if len(filtered) < 2:
        return WaterLevelSegments([], [], [], [], float(water_level))

    polyline_segments: List[_PolylineSegment] = []
    total_distance = 0.
    for index in range(len(filtered) - 1):
        start = filtered[index]
        end = filtered[index + 1]
        length = _distance(start, end)
        polyline_segments.append(_PolylineSegment(index, start, end,
                                                  total_distance, length))
        total_distance += length

    accessor = _CellAccessor(elevation_data, cell_size=cell_size,
                             elevation_function=elevation_function)
    pieces: List[_Piece] = []
    for segment in polyline_segments:
        pieces.extend(_pieces_for_segment(segment, accessor,
                                          float(water_level), tolerance))
    pieces = _merge_pieces(pieces)

    visible_kinds: Tuple[str, ...] = (ABOVE,)
    if include_contours:
        visible_kinds += (CONTOUR,)
    if include_below:
        visible_kinds += (BELOW,)

    crossings: List[WaterCrossing] = []
    for index in range(1, len(pieces)):
        incoming = pieces[index - 1].kind
        outgoing = pieces[index].kind
        if incoming == MISSING or outgoing == MISSING:
            # A data hole is not a waterline crossing.
            continue
        direction = _crossing_direction(incoming, outgoing)
        if direction is not None:
            boundary = pieces[index].start
            distance = pieces[index].start_distance
            crossings.append(WaterCrossing(boundary, distance, direction,
                                           incoming, outgoing))

    output: List[WaterLevelSegment] = []
    missing: List[MissingSegment] = []
    intervals: List[Any] = []
    for index, piece in enumerate(pieces):
        previous = pieces[index - 1] if index > 0 else None
        following = pieces[index + 1] if index + 1 < len(pieces) else None

        if piece.kind == MISSING:
            missing_segment = MissingSegment(piece.start, piece.end, piece.length,
                                             piece.start_distance, piece.end_distance)
            missing.append(missing_segment)
            intervals.append(missing_segment)
            continue

        segment = _to_segment(piece, previous, following, crossings)
        intervals.append(segment)
        if piece.kind in visible_kinds:
            output.append(segment)

    crossings.sort(key=lambda crossing: crossing.distance)
    return WaterLevelSegments(output, missing, crossings, intervals,
                              float(water_level), total_distance)


def find_water_level_segments(elevation_data: Any, points: Any,
                              water_level: float, **kwargs: Any) -> WaterLevelSegments:
    return get_water_level_segments(elevation_data, points, water_level, **kwargs)


def get_water_segments(elevation_data: Any, points: Any,
                       water_level: float, **kwargs: Any) -> WaterLevelSegments:
    return get_water_level_segments(elevation_data, points, water_level, **kwargs)


def water_level_segments(elevation_data: Any, points: Any,
                         water_level: float, **kwargs: Any) -> WaterLevelSegments:
    return get_water_level_segments(elevation_data, points, water_level, **kwargs)


def get_segments_above_water(elevation_data: Any, points: Any,
                             water_level: float, **kwargs: Any) -> WaterLevelSegments:
    return get_water_level_segments(elevation_data, points, water_level, **kwargs)


def get_above_water_segments(elevation_data: Any, points: Any,
                             water_level: float, **kwargs: Any) -> WaterLevelSegments:
    return get_water_level_segments(elevation_data, points, water_level, **kwargs)


def segments_above_water(elevation_data: Any, points: Any,
                         water_level: float, **kwargs: Any) -> WaterLevelSegments:
    return get_water_level_segments(elevation_data, points, water_level, **kwargs)


WaterSegment = WaterLevelSegment
WaterLevelSection = WaterLevelSegment
WaterLevelInterval = WaterLevelSegment
AboveWaterSegment = WaterLevelSegment
ContourSegment = WaterLevelSegment
Crossing = WaterCrossing
