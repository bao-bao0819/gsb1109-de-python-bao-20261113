# -*- coding: utf-8 -*-

"""
End-to-end demonstration of srtm.clearance on a synthetic survey line.

The raster is built in memory, so no network access is needed.  It contains
a Gaussian hill (the dangerous spot) and a band of void cells.  Wire
stations deliberately straddle the hill so the worst point falls between
two sampled stations.
"""

import math
import os
import struct
import tempfile
from typing import List

from srtm import data as mod_data
from srtm import utils as mod_utils
from srtm import clearance as mod_clearance

TILE_NAME = 'N44W072.hgt'
SIDE = 201
BASE_ELEVATION = 100.0
HILL_PEAK = 30.0
HILL_CENTER = (44.50, -71.50)
HILL_SPREAD = 0.0035
VOID_LAT_FROM, VOID_LAT_TO = 44.545, 44.555

LINE_LAT_FROM, LINE_LAT_TO = 44.45, 44.58
LINE_LON = -71.50
WIRE_ELEVATION = 117.0
WIRE_SIGMA = 0.4
RASTER_SIGMA = 2.0
REQUIRED_CLEARANCE = 8.0


def ground_truth(latitude: float, longitude: float) -> float:
    d_lat = latitude - HILL_CENTER[0]
    d_lon = longitude - HILL_CENTER[1]
    hill = HILL_PEAK * math.exp(
        -(d_lat * d_lat + d_lon * d_lon) / (2 * HILL_SPREAD * HILL_SPREAD))
    return BASE_ELEVATION + hill


def build_tile() -> mod_data.GeoElevationFile:
    resolution = 1.0 / (SIDE - 1)
    values = []
    for row in range(SIDE):
        latitude = 45.0 - row * resolution
        for column in range(SIDE):
            longitude = -72.0 + column * resolution
            if VOID_LAT_FROM <= latitude <= VOID_LAT_TO:
                values.append(-32768)  # SRTM void sentinel -> None
            else:
                values.append(int(round(ground_truth(latitude, longitude))))
    raw = struct.pack('>%dh' % len(values), *values)
    handler = mod_utils.FileHandler(tempfile.mkdtemp(prefix='clearance-demo-'))
    tile_map = mod_data.GeoElevationData({}, {}, file_handler=handler)
    return mod_data.GeoElevationFile(TILE_NAME, raw, tile_map)


def build_stations() -> List[mod_clearance.Station]:
    stations = []
    for mileage in (0.0, 300.0, 600.0, 900.0, 1200.0):
        t = mileage / 1200.0
        latitude = LINE_LAT_FROM + t * (LINE_LAT_TO - LINE_LAT_FROM)
        stations.append(mod_clearance.Station(
            mileage=mileage, latitude=latitude, longitude=LINE_LON,
            wire_elevation=WIRE_ELEVATION, wire_sigma=WIRE_SIGMA))
    return stations


def main() -> None:
    tile = build_tile()
    stations = build_stations()
    result = mod_clearance.analyze_clearance(
        stations, tile,
        required_clearance=REQUIRED_CLEARANCE,
        confidence=1.96,
        raster_sigma=RASTER_SIGMA,
        void_sigma=3.0,
        min_support=2,
        wire_error_mode='rss')

    print('required clearance: %.1f m | confidence k=%.2f | sample step: %.1f m'
          % (result.required_clearance, result.confidence, result.step))
    print()
    print('%-10s %-12s %-12s %-30s %s' %
          ('status', 'from(m)', 'to(m)', 'worst clearance band [low, high]',
           'worst at(m)'))
    for segment in result.segments:
        band = ('%-7.2f .. %7.2f' % (segment.worst_clearance_low,
                                     segment.worst_clearance_high)
                if segment.worst_clearance_low is not None
                and segment.worst_clearance_high is not None else 'void')
        print('%-10s %-12.1f %-12.1f %-30s %.1f  %s' %
              (mod_clearance.STATUS_LABELS[segment.status],
               segment.start_mileage, segment.end_mileage, band,
               segment.worst_mileage, segment.note))

    print()
    print('status changes (surveyors: bracket is positional uncertainty):')
    for transition in result.transitions:
        print('  %s -> %s within %.1f .. %.1f m (+-%.1f m) | %s'
              % (transition.from_status, transition.to_status,
                 transition.mileage_left, transition.mileage_right,
                 transition.position_half_width, transition.action))

    output_dir = os.path.join(os.path.dirname(__file__), 'demo_output')
    segments_path, transitions_path = mod_clearance.write_reports(
        result, output_dir)
    print()
    print('reports written:')
    print(' ', segments_path)
    print(' ', transitions_path)


if __name__ == '__main__':
    main()
