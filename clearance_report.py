# -*- coding: utf-8 -*-

"""
Command line clearance report.

Example:
    python clearance_report.py stations.csv \
        --hgt-dir test_files --required-clearance 8.0 \
        --raster-sigma 2.0 --void-sigma 3.0 --out reports

stations.csv header:
    mileage,latitude,longitude,wire_elevation,wire_sigma
"""

import argparse
import glob
import os
import tempfile
from typing import Dict, List

from srtm import data as mod_data
from srtm import utils as mod_utils
from srtm import clearance as mod_clearance


def load_tile_map(hgt_paths: List[str]) -> mod_data.GeoElevationData:
    handler = mod_utils.FileHandler(tempfile.mkdtemp(prefix='clearance-cli-'))
    tile_map = mod_data.GeoElevationData({}, {}, file_handler=handler)
    for path in hgt_paths:
        with open(path, 'rb') as handle:
            raw = handle.read()
        name = os.path.basename(path)
        tile = mod_data.GeoElevationFile(name, raw, tile_map)
        tile_map.files[name] = tile
        tile_map.srtm3_files[name] = ''
    return tile_map


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('stations_csv',
                        help='CSV of surveyed wire stations')
    parser.add_argument('--hgt', action='append', default=[],
                        help='local .hgt raster file (repeatable)')
    parser.add_argument('--hgt-dir', action='append', default=[],
                        help='directory scanned for .hgt files (repeatable)')
    parser.add_argument('--required-clearance', type=float, required=True,
                        help='minimum acceptable wire-to-ground distance (m)')
    parser.add_argument('--raster-sigma', type=float, default=0.0,
                        help='raster 1-sigma elevation error (m)')
    parser.add_argument('--void-sigma', type=float, default=0.0,
                        help='extra 1-sigma penalty per missing cross cell (m)')
    parser.add_argument('--min-support', type=int, default=2,
                        help='minimum valid IDW cross cells (default 2)')
    parser.add_argument('--k', type=float, default=1.96,
                        help='confidence multiplier on the combined sigma '
                             '(1.96 = 95%% band)')
    parser.add_argument('--step', type=float, default=None,
                        help='sampling distance in metres (default: shortest '
                             'station interval / 25)')
    parser.add_argument('--wire-error-mode', choices=('rss', 'envelope'),
                        default='rss',
                        help='rss: independent station errors; envelope: '
                             'conservative linear error envelope')
    parser.add_argument('--out', default='clearance_report',
                        help='output directory for the two CSV reports')
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    hgt_paths = list(args.hgt)
    for directory in args.hgt_dir:
        hgt_paths.extend(sorted(glob.glob(os.path.join(directory, '*.hgt'))))
    if not hgt_paths:
        raise SystemExit('Provide at least one --hgt file or --hgt-dir')

    stations = mod_clearance.load_stations(args.stations_csv)
    tile_map = load_tile_map(hgt_paths)
    result = mod_clearance.analyze_clearance(
        stations, tile_map,
        required_clearance=args.required_clearance,
        confidence=args.k,
        step=args.step,
        raster_sigma=args.raster_sigma,
        void_sigma=args.void_sigma,
        min_support=args.min_support,
        wire_error_mode=args.wire_error_mode)

    segments_path, transitions_path = mod_clearance.write_reports(
        result, args.out)

    counts: Dict[str, int] = {}
    for segment in result.segments:
        counts[segment.status] = counts.get(segment.status, 0) + 1
    print('sampled %d points, step %.2f m, %d segment(s)'
          % (len(result.samples), result.step, len(result.segments)))
    for status in (mod_clearance.DEFICIENT, mod_clearance.POSSIBLE,
                   mod_clearance.SUFFICIENT, mod_clearance.UNKNOWN):
        if status in counts:
            print('  %-10s %s: %d segment(s)'
                  % (status, mod_clearance.STATUS_LABELS[status],
                     counts[status]))
    print('reports:')
    print(' ', segments_path)
    print(' ', transitions_path)


if __name__ == '__main__':
    main()
