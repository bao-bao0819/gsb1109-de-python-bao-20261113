# -*- coding: utf-8 -*-

"""
Wire-to-ground clearance analysis along a survey line.

The survey line gives conductor (wire) elevations and their measurement
errors at sampled stations.  Ground elevations come from a local raster
handled by this package (a :class:`~srtm.data.GeoElevationFile` or
:class:`~srtm.data.GeoElevationData`); raster cells carry their own error
and some cells may be void.

Interpolation rules used here:

* Between two wire stations the wire elevation is linearly interpolated in
  mileage.  Wire uncertainty is propagated assuming independent station
  errors (root-sum-square); ``wire_error_mode='envelope'`` gives the
  conservative linear error envelope instead.
* Ground values *inside a raster cell* use exactly the package's specified
  in-cell interpolation: inverse distance weighting over the cell centre
  and its four cross neighbours (``_InverseDistanceWeighted`` with
  ``radius=1``).  The numeric result is bit-for-bit equivalent to
  :meth:`srtm.data.GeoElevationFile._InverseDistanceWeighted` (covered by
  tests); this module additionally exposes the normalized IDW weights so
  the raster error can be propagated.
* Void raster cells are skipped.  They are never replaced by zero
  elevation.  If fewer than ``min_support`` cross cells hold a value the
  ground elevation at that mileage is marked :data:`UNKNOWN`, which forces
  a re-survey recommendation instead of a fake "safe" result.

Classification of a point with clearance ``c = wire - ground``, combined
1-sigma error ``sigma`` and confidence factor ``k``:

* :data:`DEFICIENT`  ("肯定不足"): ``c + k*sigma < required_clearance``
* :data:`SUFFICIENT` ("确定足够"): ``c - k*sigma >= required_clearance``
* :data:`POSSIBLE`   ("可能不足"): the error band straddles the requirement
* :data:`UNKNOWN`    ("数据不足/空洞"): the raster cannot support a value

Contiguous samples with the same status are merged into mileage segments
and every status change is reported as a mileage bracket (the bracket width
is the positional uncertainty of the change point) together with the
clearance error bands on both sides.
"""

import csv as mod_csv
import math as mod_math
import os as mod_os
from dataclasses import dataclass, field
from typing import *

from . import data as mod_data
from . import utils as mod_utils

# Status constants ---------------------------------------------------------

DEFICIENT = 'DEFICIENT'
POSSIBLE = 'POSSIBLE'
SUFFICIENT = 'SUFFICIENT'
UNKNOWN = 'UNKNOWN'

STATUS_LABELS = {
    DEFICIENT: '肯定不足',
    POSSIBLE: '可能不足',
    SUFFICIENT: '确定足够',
    UNKNOWN: '数据不足(空洞)',
}

# radius=1 cross: north, west, centre, east, south -- same order/geometry as
# GeoElevationFile._InverseDistanceWeighted.
_CROSS_OFFSETS = ((0, 1), (-1, 0), (0, 0), (1, 0), (0, -1))


# Data records --------------------------------------------------------------

@dataclass
class Station:
    """One surveyed wire station."""
    mileage: float
    latitude: float
    longitude: float
    wire_elevation: float
    wire_sigma: float


@dataclass
class Sample:
    """Clearance evaluation at one mileage along the line."""
    mileage: float
    latitude: float
    longitude: float
    wire_elevation: float
    wire_sigma: float
    ground_elevation: Optional[float]
    ground_sigma: Optional[float]
    clearance: Optional[float]
    clearance_sigma: Optional[float]
    clearance_low: Optional[float]
    clearance_high: Optional[float]
    valid_cells: int
    void_cells: int
    off_tile_cells: int
    status: str
    note: str = ''


@dataclass
class Segment:
    """A maximal contiguous mileage range with one clearance status."""
    status: str
    start_mileage: float
    end_mileage: float
    worst_mileage: float
    worst_clearance: Optional[float]
    worst_clearance_low: Optional[float]
    worst_clearance_high: Optional[float]
    worst_sigma: Optional[float]
    min_valid_cells: int
    note: str = ''


@dataclass
class Transition:
    """A status change bracketed between two evaluated samples."""
    from_status: str
    to_status: str
    mileage_left: float
    mileage_right: float
    position_half_width: float
    band_left: Tuple[Optional[float], Optional[float]]
    band_right: Tuple[Optional[float], Optional[float]]
    action: str


@dataclass
class ClearanceResult:
    required_clearance: float
    confidence: float
    step: float
    samples: List[Sample] = field(default_factory=list)
    segments: List[Segment] = field(default_factory=list)
    transitions: List[Transition] = field(default_factory=list)


# Input loading -------------------------------------------------------------

def load_stations(csv_path: str) -> List[Station]:
    """
    Load wire stations from CSV.

    Required header columns: ``mileage,latitude,longitude,wire_elevation,
    wire_sigma`` (a header row is mandatory).  ``mileage`` is in metres,
    ``wire_sigma`` is the 1-sigma station error in metres.
    """
    stations = []
    with open(csv_path, newline='', encoding='utf-8') as handle:
        for row in mod_csv.DictReader(handle):
            stations.append(Station(
                float(row['mileage']),
                float(row['latitude']),
                float(row['longitude']),
                float(row['wire_elevation']),
                float(row['wire_sigma']),
            ))
    if len(stations) < 2:
        raise ValueError('At least two wire stations are required')
    stations.sort(key=lambda station: station.mileage)
    for earlier, later in zip(stations, stations[1:]):
        if later.mileage <= earlier.mileage:
            raise ValueError('Station mileages must be strictly increasing')
    return stations


# Wire interpolation --------------------------------------------------------

def interpolate_wire(stations: List[Station], mileage: float,
                     mode: str = 'rss') -> Tuple[float, float]:
    """
    Piecewise-linear wire elevation and its 1-sigma error at ``mileage``.

    ``mode='rss'`` propagates independent station errors as
    ``sqrt(((1-t)*s0)^2 + (t*s1)^2)``; ``mode='envelope'`` uses the linear
    error envelope ``(1-t)*s0 + t*s1``.
    """
    if mileage <= stations[0].mileage:
        return stations[0].wire_elevation, stations[0].wire_sigma
    if mileage >= stations[-1].mileage:
        last = stations[-1]
        return last.wire_elevation, last.wire_sigma
    for first, second in zip(stations, stations[1:]):
        span = second.mileage - first.mileage
        if first.mileage <= mileage <= second.mileage:
            t = (mileage - first.mileage) / span
            elevation = (1 - t) * first.wire_elevation + t * second.wire_elevation
            if mode == 'envelope':
                sigma = (1 - t) * first.wire_sigma + t * second.wire_sigma
            elif mode == 'rss':
                sigma = mod_math.hypot((1 - t) * first.wire_sigma,
                                       t * second.wire_sigma)
            else:
                raise ValueError("wire_error_mode must be 'rss' or 'envelope'")
            return elevation, sigma
    raise RuntimeError('Unreachable mileage %r' % mileage)


# Ground IDW with uncertainty ----------------------------------------------

def idw_ground(tile: mod_data.GeoElevationFile, latitude: float, longitude: float,
               raster_sigma: Union[float, Callable[[int, int], float]] = 0.0,
               void_sigma: float = 0.0, min_support: int = 2
               ) -> Tuple[Optional[float], Optional[float], int, int, int]:
    """
    In-cell IDW ground elevation (radius 1) with error propagation.

    Returns ``(elevation, sigma, valid_cells, void_cells, off_tile_cells)``.
    ``elevation`` is ``None`` when the cross has fewer than ``min_support``
    valid cells; void cells are never substituted with zero.  ``sigma``
    combines the propagated raster errors of the valid cells and, for each
    missing cross cell, the optional ``void_sigma`` penalty.
    """
    row, column = tile.get_row_and_column(latitude, longitude)
    centre_lat, centre_lon = tile.get_lat_and_long(row, column)

    # Exact cell centre: the package returns that cell's raw value directly.
    if latitude == centre_lat and longitude == centre_lon:
        value = tile.get_elevation_from_row_and_column(int(row), int(column))
        if value is None:
            return None, None, 0, 1, 0
        sigma = _cell_sigma(raster_sigma, row, column)
        return float(value), sigma, 1, 0, 0

    terms: List[Tuple[float, float, int, int]] = []
    void_cells = 0
    off_tile_cells = 0
    side = tile.square_side
    for d_row, d_col in _CROSS_OFFSETS:
        neighbour_row, neighbour_col = row + d_row, column + d_col
        if not (0 <= neighbour_row < side and 0 <= neighbour_col < side):
            off_tile_cells += 1
            continue
        value = tile.get_elevation_from_row_and_column(int(neighbour_row),
                                                       int(neighbour_col))
        if value is None:
            void_cells += 1
            continue
        cell_lat = centre_lat + float(d_row) / (side - 1)
        cell_lon = centre_lon + float(d_col) / (side - 1)
        distance = mod_utils.distance(latitude, longitude, cell_lat, cell_lon)
        if distance == 0.0:
            # Query point coincides with this grid node: return its raw
            # value directly (also handles floating-point node alignment
            # where the package's own IDW would divide by zero).
            sigma = _cell_sigma(raster_sigma, neighbour_row, neighbour_col)
            return float(value), sigma, 1, void_cells, off_tile_cells
        terms.append((float(value), distance, neighbour_row, neighbour_col))

    if len(terms) < min_support:
        return None, None, len(terms), void_cells, off_tile_cells

    inverse_weights = [1.0 / distance for _, distance, _, _ in terms]
    total = sum(inverse_weights)
    elevation = sum(value * weight for (value, _, _, _), weight
                    in zip(terms, inverse_weights)) / total
    variance = 0.0
    for (_, _, cell_row, cell_col), inverse in zip(terms, inverse_weights):
        normalized = inverse / total
        cell_error = _cell_sigma(raster_sigma, cell_row, cell_col)
        variance += (normalized * cell_error) ** 2
    missing = void_cells + off_tile_cells
    variance += (missing / float(len(_CROSS_OFFSETS))) * void_sigma ** 2
    return elevation, mod_math.sqrt(variance), len(terms), void_cells, off_tile_cells


def _cell_sigma(raster_sigma: Union[float, Callable[[int, int], float]],
                row: int, column: int) -> float:
    if callable(raster_sigma):
        return float(raster_sigma(row, column))
    return float(raster_sigma)


# Classification ------------------------------------------------------------

def classify(clearance_low: Optional[float], clearance_high: Optional[float],
             required_clearance: float) -> str:
    if clearance_low is None or clearance_high is None:
        return UNKNOWN
    if clearance_high < required_clearance:
        return DEFICIENT
    if clearance_low >= required_clearance:
        return SUFFICIENT
    return POSSIBLE


# Main analysis -------------------------------------------------------------

def analyze_clearance(stations: List[Station],
                      ground_source: Union[mod_data.GeoElevationData,
                                           mod_data.GeoElevationFile],
                      required_clearance: float,
                      confidence: float = 1.96,
                      step: Optional[float] = None,
                      raster_sigma: Union[float, Callable[[int, int], float]] = 0.0,
                      void_sigma: float = 0.0,
                      min_support: int = 2,
                      wire_error_mode: str = 'rss',
                      refine_rounds: int = 3) -> ClearanceResult:
    """
    Evaluate clearance along the whole line and build segment/transition
    reports.

    Arguments:
        stations: surveyed wire stations (mileage, coordinates, elevation,
            1-sigma error).
        ground_source: raster tile or tile collection used for IDW ground
            elevations.
        required_clearance: minimum acceptable wire-to-ground distance.
        confidence: multiplier ``k`` applied to the combined 1-sigma error
            (1.96 = 95% two-sided band).
        step: sampling distance in metres.  Defaults to one 25th of the
            shortest station interval, so the worst point can always fall
            between two stations.
        raster_sigma: raster 1-sigma elevation error, either a scalar or a
            callable ``sigma(row, column)``.
        void_sigma: extra 1-sigma penalty added per missing cross cell.
        min_support: minimum number of valid cross cells below which the
            mileage is marked :data:`UNKNOWN`.
        wire_error_mode: ``rss`` (independent station errors) or
            ``envelope`` (conservative linear error envelope).
        refine_rounds: bisection rounds used to sharpen each segment's
            worst-clearance location.
    """
    stations = sorted(stations, key=lambda candidate: candidate.mileage)
    if len(stations) < 2:
        raise ValueError('At least two wire stations are required')
    for earlier, later in zip(stations, stations[1:]):
        if later.mileage <= earlier.mileage:
            raise ValueError('Station mileages must be strictly increasing')

    shortest = min(second.mileage - first.mileage
                   for first, second in zip(stations, stations[1:]))
    if step is None:
        step = shortest / 25.0
    step = float(step)
    if step <= 0:
        raise ValueError('step must be positive')

    result = ClearanceResult(required_clearance=required_clearance,
                             confidence=confidence, step=step)

    for first, second in zip(stations, stations[1:]):
        subdivisions = max(1, int(mod_math.ceil(
            (second.mileage - first.mileage) / step)))
        for index in range(subdivisions):
            t = index / subdivisions
            mileage = first.mileage + t * (second.mileage - first.mileage)
            latitude = first.latitude + t * (second.latitude - first.latitude)
            longitude = first.longitude + t * (second.longitude - first.longitude)
            result.samples.append(_evaluate(
                mileage, latitude, longitude, stations, ground_source,
                required_clearance, confidence, raster_sigma, void_sigma,
                min_support, wire_error_mode))
    last = stations[-1]
    result.samples.append(_evaluate(
        last.mileage, last.latitude, last.longitude, stations, ground_source,
        required_clearance, confidence, raster_sigma, void_sigma,
        min_support, wire_error_mode))

    result.segments = merge_segments(result.samples, required_clearance,
                                     refine_rounds, stations, ground_source,
                                     confidence, raster_sigma, void_sigma,
                                     min_support, wire_error_mode)
    result.transitions = build_transitions(result.samples, required_clearance)
    return result


def _evaluate(mileage: float, latitude: float, longitude: float,
              stations: List[Station],
              ground_source: Union[mod_data.GeoElevationData,
                                   mod_data.GeoElevationFile],
              required_clearance: float, confidence: float,
              raster_sigma: Union[float, Callable[[int, int], float]],
              void_sigma: float, min_support: int,
              wire_error_mode: str) -> Sample:
    wire, wire_sigma = interpolate_wire(stations, mileage, wire_error_mode)

    tile = None
    if isinstance(ground_source, mod_data.GeoElevationData):
        tile = ground_source.get_file(latitude, longitude)
    else:
        tile = ground_source

    note = ''
    if tile is None:
        ground = ground_sigma = None
        valid_cells = void_cells = off_tile_cells = 0
        note = 'no raster tile covers this location'
    else:
        ground, ground_sigma, valid_cells, void_cells, off_tile_cells = \
            idw_ground(tile, latitude, longitude, raster_sigma,
                       void_sigma, min_support)
        if ground is None:
            note = 'raster void: only %d valid cross cell(s)' % valid_cells

    if ground is None or ground_sigma is None:
        clearance = sigma = low = high = None
        status = UNKNOWN
    else:
        clearance = wire - ground
        sigma = mod_math.hypot(wire_sigma, ground_sigma)
        low = clearance - confidence * sigma
        high = clearance + confidence * sigma
        status = classify(low, high, required_clearance)

    return Sample(mileage, latitude, longitude, wire, wire_sigma,
                  ground, ground_sigma, clearance, sigma, low, high,
                  valid_cells, void_cells, off_tile_cells, status, note)


def merge_segments(samples: List[Sample], required_clearance: float,
                   refine_rounds: int, stations: List[Station],
                   ground_source: Union[mod_data.GeoElevationData,
                                        mod_data.GeoElevationFile],
                   confidence: float,
                   raster_sigma: Union[float, Callable[[int, int], float]],
                   void_sigma: float, min_support: int,
                   wire_error_mode: str) -> List[Segment]:
    segments: List[Segment] = []
    if not samples:
        return segments

    group: List[Sample] = [samples[0]]
    for sample in samples[1:]:
        if sample.status == group[-1].status:
            group.append(sample)
        else:
            segments.append(_build_segment(group, refine_rounds, stations,
                                           ground_source, confidence,
                                           raster_sigma, void_sigma,
                                           min_support, wire_error_mode))
            group = [sample]
    segments.append(_build_segment(group, refine_rounds, stations,
                                   ground_source, confidence, raster_sigma,
                                   void_sigma, min_support, wire_error_mode))
    return segments


def _build_segment(group: List[Sample], refine_rounds: int,
                   stations: List[Station],
                   ground_source: Union[mod_data.GeoElevationData,
                                        mod_data.GeoElevationFile],
                   confidence: float,
                   raster_sigma: Union[float, Callable[[int, int], float]],
                   void_sigma: float, min_support: int,
                   wire_error_mode: str) -> Segment:
    status = group[0].status
    start_mileage = group[0].mileage
    end_mileage = group[-1].mileage
    min_support_seen = min(sample.valid_cells for sample in group)
    note = '; '.join(sorted({sample.note for sample in group if sample.note}))

    if status == UNKNOWN:
        return Segment(status, start_mileage, end_mileage, start_mileage,
                       None, None, None, None, min_support_seen, note)

    # The "worst" point is the smallest lower clearance band (most dangerous
    # even under favourable errors).  Dense sampling already brackets it
    # within one grid step; bisection sharpens the location without assuming
    # the clearance curve is unimodal.
    worst = min(group, key=_lower_band)
    left = max(start_mileage, worst.mileage - _neighbour_span(group, worst))
    right = min(end_mileage, worst.mileage + _neighbour_span(group, worst))
    for _ in range(max(0, refine_rounds)):
        if right <= left:
            break
        middle = 0.5 * (left + right)
        candidate = _evaluate_at_mileage(
            middle, stations, ground_source, confidence, raster_sigma,
            void_sigma, min_support, wire_error_mode)
        candidate_low = candidate.clearance_low
        if candidate.status != UNKNOWN and candidate_low is not None \
                and candidate_low < _lower_band(worst):
            worst = candidate
            left, right = max(left, middle - (right - left) / 4.0), \
                min(right, middle + (right - left) / 4.0)
        elif middle < worst.mileage:
            left = middle
        else:
            right = middle

    return Segment(status, start_mileage, end_mileage, worst.mileage,
                   worst.clearance, worst.clearance_low, worst.clearance_high,
                   worst.clearance_sigma, min_support_seen, note)


def _lower_band(sample: Sample) -> float:
    assert sample.clearance_low is not None
    return sample.clearance_low


def _neighbour_span(group: List[Sample], sample: Sample) -> float:
    index = group.index(sample)
    if index == 0:
        if len(group) == 1:
            return 0.0
        return group[1].mileage - sample.mileage
    if index == len(group) - 1:
        return sample.mileage - group[index - 1].mileage
    return max(sample.mileage - group[index - 1].mileage,
               group[index + 1].mileage - sample.mileage)


def _evaluate_at_mileage(mileage: float, stations: List[Station],
                         ground_source: Union[mod_data.GeoElevationData,
                                              mod_data.GeoElevationFile],
                         confidence: float,
                         raster_sigma: Union[float, Callable[[int, int], float]],
                         void_sigma: float, min_support: int,
                         wire_error_mode: str) -> Sample:
    position = _position_at_mileage(stations, mileage)
    required = 0.0  # status irrelevant for extremum search
    return _evaluate(mileage, position[0], position[1], stations,
                     ground_source, required, confidence, raster_sigma,
                     void_sigma, min_support, wire_error_mode)


def _position_at_mileage(stations: List[Station],
                         mileage: float) -> Tuple[float, float]:
    if mileage <= stations[0].mileage:
        return stations[0].latitude, stations[0].longitude
    if mileage >= stations[-1].mileage:
        last = stations[-1]
        return last.latitude, last.longitude
    for first, second in zip(stations, stations[1:]):
        if first.mileage <= mileage <= second.mileage:
            t = ((mileage - first.mileage) /
                 (second.mileage - first.mileage))
            return (first.latitude + t * (second.latitude - first.latitude),
                    first.longitude + t * (second.longitude - first.longitude))
    raise RuntimeError('Unreachable mileage %r' % mileage)


def build_transitions(samples: List[Sample],
                      required_clearance: float) -> List[Transition]:
    transitions: List[Transition] = []
    for left, right in zip(samples, samples[1:]):
        if left.status == right.status:
            continue
        needs_resurvey = UNKNOWN in (left.status, right.status) or \
            POSSIBLE in (left.status, right.status)
        action = ('派人复测:误差带跨越净空限值或存在栅格空洞'
                  if needs_resurvey else
                  '证据已足:按括号里程现场核定变化点即可')
        transitions.append(Transition(
            left.status, right.status, left.mileage, right.mileage,
            0.5 * (right.mileage - left.mileage),
            (left.clearance_low, left.clearance_high),
            (right.clearance_low, right.clearance_high), action))
    return transitions


# Reporting -----------------------------------------------------------------

def write_reports(result: ClearanceResult, output_dir: str) -> Tuple[str, str]:
    """Write segment and transition CSVs; return their paths."""
    mod_os.makedirs(output_dir, exist_ok=True)
    segments_path = mod_os.path.join(output_dir, 'clearance_segments.csv')
    with open(segments_path, 'w', newline='', encoding='utf-8') as handle:
        writer = mod_csv.writer(handle)
        writer.writerow([
            'status', 'status_label', 'start_mileage_m', 'end_mileage_m',
            'length_m', 'worst_mileage_m', 'worst_clearance_m',
            'worst_clearance_low_m', 'worst_clearance_high_m',
            'worst_sigma_m', 'min_valid_cells', 'note',
        ])
        for segment in result.segments:
            writer.writerow([
                segment.status, STATUS_LABELS[segment.status],
                _fmt(segment.start_mileage), _fmt(segment.end_mileage),
                _fmt(segment.end_mileage - segment.start_mileage),
                _fmt(segment.worst_mileage), _fmt(segment.worst_clearance),
                _fmt(segment.worst_clearance_low),
                _fmt(segment.worst_clearance_high), _fmt(segment.worst_sigma),
                segment.min_valid_cells, segment.note,
            ])

    transitions_path = mod_os.path.join(output_dir, 'clearance_transitions.csv')
    with open(transitions_path, 'w', newline='', encoding='utf-8') as handle:
        writer = mod_csv.writer(handle)
        writer.writerow([
            'from_status', 'to_status', 'mileage_left_m', 'mileage_right_m',
            'position_half_width_m', 'left_band_low_m', 'left_band_high_m',
            'right_band_low_m', 'right_band_high_m', 'recommended_action',
        ])
        for transition in result.transitions:
            writer.writerow([
                transition.from_status, transition.to_status,
                _fmt(transition.mileage_left), _fmt(transition.mileage_right),
                _fmt(transition.position_half_width),
                _fmt(transition.band_left[0]), _fmt(transition.band_left[1]),
                _fmt(transition.band_right[0]), _fmt(transition.band_right[1]),
                transition.action,
            ])
    return segments_path, transitions_path


def _fmt(value: Optional[float]) -> str:
    if value is None:
        return ''
    return '%.3f' % value
