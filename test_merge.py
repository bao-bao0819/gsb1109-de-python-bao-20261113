# -*- coding: utf-8 -*-

# Copyright 2013 Tomo Krajina
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Run all tests with:
    $ python -m unittest test_merge
"""

import struct           as mod_struct
import unittest         as mod_unittest

from srtm import data   as mod_data
from srtm import merge  as mod_merge
from srtm import utils  as mod_utils

VOID = -32768


def make_file(file_name: str, square_side: int=3, fill: int=100, overrides=None) -> mod_data.GeoElevationFile:
    """ Builds a synthetic tile with big-endian short samples. """
    values = [[fill] * square_side for _ in range(square_side)]
    for (row, column), value in (overrides or {}).items():
        values[row][column] = value
    data = b''.join(mod_struct.pack('>h', value) for row in values for value in row)
    geo_elevation_data = mod_data.GeoElevationData({}, {}, file_handler=mod_utils.FileHandler(''))
    return mod_data.GeoElevationFile(file_name, data, geo_elevation_data)


class MergeTests(mod_unittest.TestCase):

    def test_shared_edge_is_deduplicated(self) -> None:
        # N00E000 and N00E001 share the edge column at longitude 1:
        grid = mod_merge.MergedElevationGrid([
            make_file('N00E000.hgt', fill=100),
            make_file('N00E001.hgt', fill=100),
        ])

        # 3 rows x (3 + 3 - 1) columns, the shared edge stored only once:
        self.assertEqual(15, len(grid))

        record = grid.get_record(0.5, 1.0)
        self.assertEqual(100, record.value) # type: ignore
        self.assertEqual(2, record.evidence_count) # type: ignore
        self.assertEqual({'N00E000.hgt', 'N00E001.hgt'}, record.sources) # type: ignore
        self.assertFalse(record.is_conflict) # type: ignore

    def test_four_tile_corner(self) -> None:
        files = [
            make_file('N00E000.hgt', fill=200),
            make_file('N00W001.hgt', fill=200),
            make_file('S01E000.hgt', fill=200),
            make_file('S01W001.hgt', fill=200),
        ]
        grid = mod_merge.MergedElevationGrid(files)

        # 5 x 5 unique grid points from 4 tiles of 3 x 3:
        self.assertEqual(25, len(grid))

        corner = grid.get_record(0.0, 0.0)
        self.assertEqual(200, corner.value) # type: ignore
        self.assertEqual(4, corner.evidence_count) # type: ignore
        self.assertEqual(4, len(corner.sources)) # type: ignore

        # Points on the shared edges (but not the corner) have 2 sources:
        edge = grid.get_record(0.5, 0.0)
        self.assertEqual(2, edge.evidence_count) # type: ignore

    def test_load_order_does_not_matter(self) -> None:
        names = ['N00E000.hgt', 'N00W001.hgt', 'S01E000.hgt', 'S01W001.hgt']
        grids = [
            mod_merge.MergedElevationGrid([make_file(name, fill=200) for name in names]),
            mod_merge.MergedElevationGrid([make_file(name, fill=200) for name in reversed(names)]),
        ]

        corners = [grid.get_record(0.0, 0.0) for grid in grids]
        self.assertEqual(corners[0].value, corners[1].value) # type: ignore
        self.assertEqual(corners[0].evidence_count, corners[1].evidence_count) # type: ignore
        self.assertEqual(corners[0].sources, corners[1].sources) # type: ignore

    def test_void_does_not_cancel_valid_value(self) -> None:
        # N00E000 has a void on the shared edge, N00E001 measured 150 there:
        grid = mod_merge.MergedElevationGrid([
            make_file('N00E000.hgt', overrides={(1, 2): VOID}),
            make_file('N00E001.hgt', overrides={(1, 0): 150}),
        ])

        record = grid.get_record(0.5, 1.0)
        self.assertEqual(150, record.value) # type: ignore
        self.assertEqual(2, record.evidence_count) # type: ignore
        self.assertEqual({'N00E000.hgt'}, record.void_sources) # type: ignore
        self.assertFalse(record.is_conflict) # type: ignore

    def test_conflicting_values_left_for_adjudication(self) -> None:
        grid = mod_merge.MergedElevationGrid([
            make_file('N00E000.hgt', overrides={(1, 2): 100}),
            make_file('N00E001.hgt', overrides={(1, 0): 300}),
        ])

        record = grid.get_record(0.5, 1.0)
        self.assertTrue(record.is_conflict) # type: ignore
        self.assertEqual(None, record.value) # type: ignore
        self.assertEqual([100, 300], record.values) # type: ignore
        self.assertEqual(2, record.evidence_count) # type: ignore
        self.assertEqual([record], grid.conflicts())

    def test_east_west_hemispheres_align_at_zero_meridian(self) -> None:
        grid = mod_merge.MergedElevationGrid([
            make_file('N00W001.hgt', fill=50),
            make_file('N00E000.hgt', fill=50),
        ])

        self.assertEqual(15, len(grid))

        record = grid.get_record(0.5, 0.0)
        self.assertEqual(50, record.value) # type: ignore
        self.assertEqual({'N00W001.hgt', 'N00E000.hgt'}, record.sources) # type: ignore

    def test_different_resolutions_not_merged(self) -> None:
        grid = mod_merge.MergedElevationGrid([
            make_file('N00E000.hgt', square_side=3), # resolution 1/2
            make_file('N00E000.hgt', square_side=5), # resolution 1/4
        ])

        self.assertEqual(9 + 25, len(grid))
        self.assertEqual({0.5, 0.25}, grid.resolutions)

        record = grid.get_record(0.5, 0.5, resolution=0.25)
        self.assertEqual(100, record.value) # type: ignore
        self.assertEqual(1, record.evidence_count) # type: ignore

    def test_off_grid_coordinates_rejected(self) -> None:
        grid = mod_merge.MergedElevationGrid([make_file('N00E000.hgt')])
        self.assertRaises(ValueError, grid.get_record, 0.3, 0.3)


if __name__ == '__main__':
    mod_unittest.main()
