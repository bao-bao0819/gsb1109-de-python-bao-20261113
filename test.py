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
    $ python -m unittest test
"""

import logging        as mod_logging
import struct         as mod_struct
import unittest       as mod_unittest
import srtm           as mod_srtm
from srtm import data as mod_data
from srtm import main as mod_main
from srtm import utils as mod_utils

mod_logging.basicConfig(level=mod_logging.DEBUG,
                        format='%(asctime)s %(name)-12s %(levelname)-8s %(message)s')


def _load_offline_tile() -> "tuple[mod_data.GeoElevationData, mod_data.GeoElevationFile]":
    """ Build a GeoElevationData containing only the bundled N44W072 tile. """
    with open("test_files/N44W072.hgt", "rb") as hgtfile:
        hgt = hgtfile.read()
    tilemap = mod_data.GeoElevationData({}, {}, file_handler=mod_utils.FileHandler(""))
    tile = mod_data.GeoElevationFile('N44W072.hgt', hgt, tilemap)
    tilemap.srtm3_files["N44W072.hgt"] = ""
    tilemap.files["N44W072.hgt"] = tile
    return tilemap, tile


# Query point used by the barrier/region tests:
TEST_LATITUDE = 44.1756325
TEST_LONGITUDE = -71.5965699

class Tests(mod_unittest.TestCase):

    def test_dead_sea(self) -> None:
        geo_elevation_data = mod_srtm.get_data()
        self.assertEqual(-415, geo_elevation_data.get_elevation(31.5, 35.5))

    def test_over_60(self) -> None:
        geo_elevation_data = mod_srtm.get_data()
        self.assertTrue(geo_elevation_data.get_elevation(55., 55.) > 0) # type: ignore
        self.assertEqual(None, geo_elevation_data.get_elevation(65., 65.))
        self.assertEqual(None, geo_elevation_data.get_elevation(75., 75.))

    def test_random_points(self) -> None:
        geo_elevation_data = mod_srtm.get_data()
        self.assertEqual(597, geo_elevation_data.get_elevation(24.056313, 35.003152))
        self.assertEqual(63, geo_elevation_data.get_elevation(46., 13.))
        self.assertEqual(2714, geo_elevation_data.get_elevation(46.999999, 13.))
        self.assertEqual(1643, geo_elevation_data.get_elevation(46.999999, 13.999999))
        self.assertEqual(553, geo_elevation_data.get_elevation(46., 13.999999))
        self.assertEqual(203, geo_elevation_data.get_elevation(45.2732, 13.7139))
        self.assertEqual(460, geo_elevation_data.get_elevation(45.287, 13.905))

    def test_around_zero_longitude(self) -> None:
        geo_elevation_data = mod_srtm.get_data()
        self.assertEqual(61, geo_elevation_data.get_elevation(51.2, 0.0))
        self.assertEqual(100, geo_elevation_data.get_elevation(51.2, -0.1))
        self.assertEqual(59, geo_elevation_data.get_elevation(51.2, 0.1))

    def test_around_zero_latitude(self) -> None:
        geo_elevation_data = mod_srtm.get_data()
        self.assertEqual(393, geo_elevation_data.get_elevation(0, 15))
        self.assertEqual(423, geo_elevation_data.get_elevation(-0.1, 15))
        self.assertEqual(381, geo_elevation_data.get_elevation(0.1, 15))

    def test_point_with_invalid_elevation(self) -> None:
        geo_elevation_data = mod_srtm.get_data()
        self.assertEqual(None, geo_elevation_data.get_elevation(47.0, 13.07))

    def test_point_without_file(self) -> None:
        geo_elevation_data = mod_srtm.get_data()
        print(geo_elevation_data.get_elevation(0, 0))

    def test_files_equality(self) -> None:
        geo_elevation_data = mod_srtm.get_data()
        self.assertEqual(geo_elevation_data.get_file(47.0, 13.99),
                          geo_elevation_data.get_file(47.0, 13.0))
        self.assertEqual(geo_elevation_data.get_file(47.99, 13.99),
                          geo_elevation_data.get_file(47.0, 13.0))

        self.assertEqual(geo_elevation_data.get_file(-47.0, 13.99),
                          geo_elevation_data.get_file(-47.0, 13.0))
        self.assertEqual(geo_elevation_data.get_file(-47.99, 13.99),
                          geo_elevation_data.get_file(-47.0, 13.0))

        self.assertEqual(geo_elevation_data.get_file(-47.0, -13.99),
                          geo_elevation_data.get_file(-47.0, -13.0))
        self.assertEqual(geo_elevation_data.get_file(-47.99, -13.99),
                          geo_elevation_data.get_file(-47.0, -13.0))

        self.assertEqual(geo_elevation_data.get_file(47.0, -13.99),
                          geo_elevation_data.get_file(47.0, -13.0))
        self.assertEqual(geo_elevation_data.get_file(47.99, -13.99),
                          geo_elevation_data.get_file(47.0, -13.0))

    def test_invalit_coordinates_for_file(self) -> None:
        geo_elevation_data = mod_srtm.get_data()
        geo_file = geo_elevation_data.get_file(47.0, 13.99)

        try:
            self.assertFalse(geo_file.get_elevation(1, 1)) # type: ignore
        except Exception as e:
            message = str(e)
            self.assertEqual('Invalid latitude 1 for file N47E013.hgt', message)

        try:
            self.assertFalse(geo_file.get_elevation(47, 1)) # type: ignore
        except Exception as e:
            message = str(e)
            self.assertEqual('Invalid longitude 1 for file N47E013.hgt', message)

    def test_invalid_file(self) -> None:
        geo_elevation_data = mod_srtm.get_data()
        geo_file = geo_elevation_data.get_file(-47.0, -13.99)
        self.assertEqual(None, geo_file)

    def test_coordinates_in_file(self) -> None:
        geo_elevation_data = mod_srtm.get_data()
        geo_file = geo_elevation_data.get_file(47.0, 13.99)

        print('file:', geo_file)

        self.assertEqual(geo_file.get_elevation(47, 13), geo_file.get_elevation(47, 13)) # type: ignore

    def test_coordinates_row_col_conversion(self) -> None:
        geo_elevation_data = mod_srtm.get_data()
        geo_file = geo_elevation_data.get_file(47.0, 13.99)

        print('file:', geo_file)

        r, c = geo_file.get_row_and_column(47, 13) # type: ignore
        lat, long = geo_file.get_lat_and_long(r, c) # type: ignore
        self.assertEqual(lat, 47)
        self.assertEqual(long, 13)

        r, c = geo_file.get_row_and_column(46.5371, 8.1264) # type: ignore
        lat, long = geo_file.get_lat_and_long(r, c) # type: ignore
        self.assertAlmostEqual(lat, 46.5371, delta=geo_file.resolution) # type: ignore
        self.assertAlmostEqual(long, 8.1264, delta=geo_file.resolution) # type: ignore

    def test_without_approximation(self) -> None:
        geo_elevation_data = mod_srtm.get_data()

        self.assertEqual(geo_elevation_data.get_elevation(47.1, 13.1, approximate=False),
                          geo_elevation_data.get_elevation(47.1, 13.1))

        # SRTM elevations are always integers:
        elevation = geo_elevation_data.get_elevation(47.1, 13.1)
        self.assertTrue(int(elevation) == elevation) # type: ignore

    def test_with_approximation(self) -> None:
        geo_elevation_data = mod_srtm.get_data()

        self.assertNotEquals(geo_elevation_data.get_elevation(47.1, 13.1, approximate=True),
                             geo_elevation_data.get_elevation(47.1, 13.1))

        # When approximating a random point, it probably won't be a integer:
        elevation = geo_elevation_data.get_elevation(47.1, 13.1, approximate=True)
        self.assertTrue(int(elevation) != elevation) # type: ignore

    def test_approximation(self) -> None:
        # TODO(TK) Better tests for approximation here:
        geo_elevation_data = mod_srtm.get_data()
        elevation_without_approximation = geo_elevation_data.get_elevation(47, 13)
        elevation_with_approximation = geo_elevation_data.get_elevation(47, 13, approximate=True)

        print(elevation_without_approximation)
        print(elevation_with_approximation)

        self.assertNotEqual(elevation_with_approximation, elevation_without_approximation)
        self.assertTrue(abs(elevation_with_approximation - elevation_without_approximation) < 30) # type: ignore

    def test_IDW(self) -> None:
        print("Testing: IDW")

        # Setup with local tile
        with open("test_files/N44W072.hgt","rb") as hgtfile:
            hgt = hgtfile.read()
        tilemap = mod_data.GeoElevationData({},{}, file_handler=mod_utils.FileHandler(""))
        tile = mod_data.GeoElevationFile('N44W072.hgt', hgt, tilemap)
        tilemap.srtm3_files["N44W072.hgt"] = ""
        tilemap.files["N44W072.hgt"] = tile
        lat, long = 44.1756325, -71.5965699
        elevation = tilemap._IDW(lat, long)
        print("Location: {}, {}".format(lat, long))
        print("Elevation: {}".format(elevation))
        print()
        self.assertLessEqual(elevation, 814)
        self.assertGreaterEqual(elevation, 801)
        self.assertTrue(tilemap._IDW(-47.0, -13.99) is None)
        

    def test_InverseDistanceWeighted(self) -> None:
        print("Testing: InverseDistanceWeighted")

        # Setup minimal tile config
        with open("test_files/N44W072.hgt","rb") as hgtfile:
            hgt = hgtfile.read()
        tilemap = mod_data.GeoElevationData({},{}, file_handler=mod_utils.FileHandler())
        tile = mod_data.GeoElevationFile('N44W072.hgt', hgt, tilemap)

        # tuples of (lat, lon, lowerbound, upperbound)
        controlpoints = [(44.1756325, -71.5965699, 801, 814), # middle of tile (x,y)
                         (44, -71.5965699, 520, 532), # bottom edge (1200, y)
                         (44.1756325, -70.99975, 148, 152), # right edge (x, 1200)
                         (44.99975, -71.5965699, 525, 538), # top edge (0, y)
                         (44.1756325, -71.99975, 272, 279), # left edge (x, 0)
                         (44, -72, 341, 341)] # Exact cell coordinates, no interpolation

        for location in controlpoints:
            print("Location: {}, {}".format(location[0], location[1]))
            nearest_neighbor_elevation = tile.get_elevation(location[0], location[1])
            IDW5_elevation = tile._InverseDistanceWeighted(location[0], location[1])
            IDW13_elevation = tile._InverseDistanceWeighted(location[0], location[1], radius=2)
            print("Nearest: " + str(nearest_neighbor_elevation))
            print("Interpolated(5): {}".format(IDW5_elevation))
            print("Interpolated(13): {}".format(IDW13_elevation))
            print()
            self.assertGreaterEqual(IDW5_elevation, location[2])
            self.assertLessEqual(IDW5_elevation, location[3])
            self.assertGreaterEqual(IDW13_elevation, location[2])
            self.assertLessEqual(IDW13_elevation, location[3])

        self.assertRaises(ValueError, tile._InverseDistanceWeighted, 44, -71, radius=0)

    def test_barrier_geometry_primitives(self) -> None:
        # Proper transversal crossing:
        self.assertTrue(mod_utils.segments_cross_strictly(
            (-1.0, -1.0), (1.0, 1.0), (-1.0, 1.0), (1.0, -1.0)))
        # A path running through a barrier endpoint (T-junction) is not a crossing:
        self.assertFalse(mod_utils.segments_cross_strictly(
            (-1.0, 0.0), (1.0, 0.0), (0.0, 0.0), (0.0, 1.0)))
        # Point on segment, including endpoints:
        self.assertTrue(mod_utils.point_on_segment((0.0, 1.0), (-1.0, 1.0), (1.0, 1.0)))
        self.assertTrue(mod_utils.point_on_segment((0.0, 0.0), (0.0, 0.0), (1.0, 1.0)))
        self.assertFalse(mod_utils.point_on_segment((2.0, 2.0), (0.0, 0.0), (1.0, 1.0)))
        # Endpoint grazing does not block, a support point sitting on the barrier does:
        self.assertFalse(mod_utils.segment_is_blocked(
            (-1.0, 0.0), (1.0, 0.0), (0.0, 0.0), (0.0, 1.0)))
        self.assertTrue(mod_utils.segment_is_blocked(
            (-1.0, 0.0), (0.0, 0.0), (0.0, 0.0), (0.0, 1.0)))
        # Collinear overlap rules:
        self.assertTrue(mod_utils.collinear_segments_overlap(
            (0.0, 0.0), (2.0, 0.0), (1.0, 0.0), (3.0, 0.0)))
        self.assertFalse(mod_utils.collinear_segments_overlap(
            (0.0, 0.0), (1.0, 0.0), (1.0, 0.0), (2.0, 0.0)))
        self.assertFalse(mod_utils.collinear_segments_overlap(
            (0.0, 0.0), (1.0, 1.0), (0.0, 1.0), (1.0, 0.0)))
        # Point in ring (boundary counts as inside):
        ring = [(0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0)]
        self.assertTrue(mod_utils.point_in_ring((0.5, 0.5), ring))
        self.assertTrue(mod_utils.point_in_ring((0.0, 0.5), ring))
        self.assertFalse(mod_utils.point_in_ring((2.0, 2.0), ring))
        # Input normalization:
        self.assertEqual(mod_utils.normalize_barriers(None), [])
        self.assertEqual(mod_utils.normalize_barriers([(1, 2), (3, 4)]),
                         [[(1.0, 2.0), (3.0, 4.0)]])
        self.assertEqual(mod_utils.normalize_valid_regions([[(1, 2), (3, 4), (5, 6)]]),
                         [[(1.0, 2.0), (3.0, 4.0), (5.0, 6.0)]])

    def test_idw_barrier_blocks_crossing_support(self) -> None:
        tilemap, tile = _load_offline_tile()
        row, column = tile.get_row_and_column(TEST_LATITUDE, TEST_LONGITUDE)
        center_lat, center_long = tile.get_lat_and_long(row, column)
        east = (center_lat, center_long + tile.resolution)
        mid_longitude = (TEST_LONGITUDE + east[1]) / 2.0
        barrier = [[(TEST_LATITUDE + 0.001, mid_longitude),
                    (TEST_LATITUDE - 0.001, mid_longitude)]]

        base = tile._InverseDistanceWeighted(TEST_LATITUDE, TEST_LONGITUDE)
        result = tile._InverseDistanceWeightedDetailed(TEST_LATITUDE, TEST_LONGITUDE,
                                               barriers=barrier)
        self.assertIsInstance(result, mod_data.IDWResult)
        self.assertEqual(result.used_points, 4)
        self.assertEqual(len(result.excluded), 1)
        excluded = result.excluded[0]
        self.assertAlmostEqual(excluded.latitude, east[0])
        self.assertAlmostEqual(excluded.longitude, east[1])
        self.assertTrue(excluded.reason.startswith('blocked_by_barrier[0].segment['))
        self.assertIsNotNone(result.elevation)
        self.assertNotEqual(result.elevation, base)
        # The plain call path applies the same filtering:
        constrained = tile._InverseDistanceWeighted(TEST_LATITUDE, TEST_LONGITUDE,
                                                     barriers=barrier)
        self.assertEqual(constrained, result.elevation)
        # No barrier means no exclusion:
        unconstrained = tile._InverseDistanceWeightedDetailed(TEST_LATITUDE, TEST_LONGITUDE,
                                                       )
        self.assertEqual(unconstrained.used_points, 5)
        self.assertEqual(unconstrained.excluded, [])
        self.assertEqual(unconstrained.elevation, base)

    def test_idw_barrier_endpoint_and_online_semantics(self) -> None:
        tilemap, tile = _load_offline_tile()
        row, column = tile.get_row_and_column(TEST_LATITUDE, TEST_LONGITUDE)
        center_lat, center_long = tile.get_lat_and_long(row, column)
        east = (center_lat, center_long + tile.resolution)
        base = tile._InverseDistanceWeighted(TEST_LATITUDE, TEST_LONGITUDE)

        # Barrier endpoint at the middle of the query->east path: grazing is allowed:
        midpoint = ((TEST_LATITUDE + east[0]) / 2.0,
                    (TEST_LONGITUDE + east[1]) / 2.0)
        graze_barrier = [[midpoint, (44.18, -71.5950)]]
        graze = tile._InverseDistanceWeightedDetailed(TEST_LATITUDE, TEST_LONGITUDE,
                                              barriers=graze_barrier)
        self.assertEqual(graze.used_points, 5)
        self.assertEqual(graze.excluded, [])
        self.assertEqual(graze.elevation, base)

        # Barrier endpoint exactly on a support point: that point must be excluded:
        endpoint_barrier = [[(44.175, -71.596), east]]
        endpoint = tile._InverseDistanceWeightedDetailed(TEST_LATITUDE, TEST_LONGITUDE,
                                                 barriers=endpoint_barrier)
        self.assertEqual(endpoint.used_points, 4)
        self.assertEqual(len(endpoint.excluded), 1)
        self.assertAlmostEqual(endpoint.excluded[0].latitude, east[0])
        self.assertAlmostEqual(endpoint.excluded[0].longitude, east[1])
        self.assertTrue('blocked_by_barrier' in endpoint.excluded[0].reason)

        # Support point lying in the interior of the barrier line:
        online_barrier = [[(44.176, -71.5955), (44.1755, -71.5965)]]
        online = tile._InverseDistanceWeightedDetailed(TEST_LATITUDE, TEST_LONGITUDE,
                                               barriers=online_barrier)
        self.assertEqual(online.used_points, 4)
        self.assertTrue(any(item.reason.startswith('blocked_by_barrier')
                            for item in online.excluded))

        # Query point itself on the barrier: departing paths do not cross it:
        query_barrier = [[(TEST_LATITUDE - 0.001, TEST_LONGITUDE),
                          (TEST_LATITUDE + 0.001, TEST_LONGITUDE)]]
        on_query = tile._InverseDistanceWeightedDetailed(TEST_LATITUDE, TEST_LONGITUDE,
                                                 barriers=query_barrier)
        self.assertEqual(on_query.used_points, 5)
        self.assertEqual(on_query.elevation, base)

    def test_idw_valid_regions(self) -> None:
        tilemap, tile = _load_offline_tile()
        row, column = tile.get_row_and_column(TEST_LATITUDE, TEST_LONGITUDE)
        center_lat, center_long = tile.get_lat_and_long(row, column)

        # Ring containing only the center grid point:
        half = tile.resolution * 0.6
        center_ring = [[(center_lat + half, center_long - half),
                        (center_lat + half, center_long + half),
                        (center_lat - half, center_long + half),
                        (center_lat - half, center_long - half)]]
        result = tile._InverseDistanceWeightedDetailed(TEST_LATITUDE, TEST_LONGITUDE,
                                               valid_regions=center_ring)
        self.assertEqual(result.used_points, 1)
        self.assertEqual(len(result.excluded), 4)
        self.assertTrue(all(item.reason == 'outside_valid_region'
                            for item in result.excluded))
        self.assertEqual(result.elevation,
                         tile.get_elevation_from_row_and_column(int(row), int(column)))

        # Ring nowhere near the samples: every support point is rejected:
        far_ring = [[(10.0, 10.0), (10.0, 11.0), (11.0, 11.0), (11.0, 10.0)]]
        empty = tile._InverseDistanceWeightedDetailed(TEST_LATITUDE, TEST_LONGITUDE,
                                              valid_regions=far_ring)
        self.assertIsNone(empty.elevation)
        self.assertEqual(empty.used_points, 0)
        self.assertEqual(len(empty.excluded), 5)
        distances = [item.distance for item in empty.excluded]
        self.assertEqual(distances, sorted(distances))

    def test_idw_insufficient_support_lists_reasons(self) -> None:
        tilemap, tile = _load_offline_tile()
        row, column = tile.get_row_and_column(TEST_LATITUDE, TEST_LONGITUDE)
        center_lat, center_long = tile.get_lat_and_long(row, column)

        # Region keeps only the center point:
        half = tile.resolution * 0.6
        center_ring = [[(center_lat + half, center_long - half),
                        (center_lat + half, center_long + half),
                        (center_lat - half, center_long + half),
                        (center_lat - half, center_long - half)]]
        # ... and a barrier cuts the path to that last point:
        mid_longitude = (TEST_LONGITUDE + center_long) / 2.0
        barrier = [[(TEST_LATITUDE + 0.001, mid_longitude),
                    (TEST_LATITUDE - 0.001, mid_longitude)]]

        result = tile._InverseDistanceWeightedDetailed(TEST_LATITUDE, TEST_LONGITUDE,
                                               barriers=barrier,
                                               valid_regions=center_ring,
                                               )
        self.assertIsNone(result.elevation)
        self.assertEqual(result.used_points, 0)
        self.assertEqual(len(result.excluded), 5)
        # Excluded candidates are reported nearest first:
        distances = [item.distance for item in result.excluded]
        self.assertEqual(distances, sorted(distances))
        reasons = [item.reason for item in result.excluded]
        self.assertEqual(sum(1 for reason in reasons if reason.startswith('blocked_by_barrier')), 1)
        self.assertEqual(sum(1 for reason in reasons if reason == 'outside_valid_region'), 4)
        # The nearest point overall is the barrier-blocked center sample:
        self.assertTrue(result.excluded[0].reason.startswith('blocked_by_barrier'))
        self.assertAlmostEqual(result.excluded[0].latitude, center_lat)
        self.assertAlmostEqual(result.excluded[0].longitude, center_long)

    def test_idw_void_support_is_excluded(self) -> None:
        with open("test_files/N44W072.hgt", "rb") as hgtfile:
            hgt = bytearray(hgtfile.read())
        tilemap = mod_data.GeoElevationData({}, {}, file_handler=mod_utils.FileHandler(""))
        tile = mod_data.GeoElevationFile('N44W072.hgt', bytes(hgt), tilemap)
        row, column = tile.get_row_and_column(TEST_LATITUDE, TEST_LONGITUDE)

        # Punch a void into the east neighbor cell:
        void_row, void_column = int(row), int(column + 1)
        index = (void_row * tile.square_side + void_column) * 2
        hgt[index:index + 2] = mod_struct.pack(">h", -32768)
        void_tile = mod_data.GeoElevationFile('N44W072.hgt', bytes(hgt), tilemap)

        result = void_tile._InverseDistanceWeightedDetailed(TEST_LATITUDE, TEST_LONGITUDE,
                                                    )
        self.assertEqual(result.used_points, 4)
        self.assertEqual(len(result.excluded), 1)
        self.assertEqual(result.excluded[0].reason, 'void')
        self.assertIsNone(result.excluded[0].elevation)
        # Legacy behavior: void cells are simply skipped, a float is still returned:
        legacy = void_tile._InverseDistanceWeighted(TEST_LATITUDE, TEST_LONGITUDE)
        self.assertIsInstance(legacy, float)

    def test_dataset_barriers_and_cross_tile_polyline(self) -> None:
        tilemap, tile = _load_offline_tile()
        row, column = tile.get_row_and_column(TEST_LATITUDE, TEST_LONGITUDE)
        center_lat, center_long = tile.get_lat_and_long(row, column)
        east = (center_lat, center_long + tile.resolution)
        mid_longitude = (TEST_LONGITUDE + east[1]) / 2.0
        local_barrier = [[(TEST_LATITUDE + 0.001, mid_longitude),
                          (TEST_LATITUDE - 0.001, mid_longitude)]]
        base = tilemap._IDW(TEST_LATITUDE, TEST_LONGITUDE)

        # Barriers configured on the dataset apply to every query:
        tilemap.set_barriers(local_barrier)
        constrained = tilemap._IDW(TEST_LATITUDE, TEST_LONGITUDE)
        self.assertIsNotNone(constrained)
        self.assertNotEqual(constrained, base)
        detailed = tilemap._IDW_detailed(TEST_LATITUDE, TEST_LONGITUDE)
        assert detailed is not None
        self.assertEqual(detailed.used_points, 4)

        # Per-query arguments override the dataset configuration:
        self.assertEqual(tilemap._IDW(TEST_LATITUDE, TEST_LONGITUDE, barriers=[]), base)
        tilemap.set_barriers(None)
        self.assertEqual(tilemap._IDW(TEST_LATITUDE, TEST_LONGITUDE), base)

        # The same barrier polyline can start in a neighboring tile:
        cross_tile_barrier = [[(44.5, -72.5), (44.5, -71.8),
                               (TEST_LATITUDE + 0.001, mid_longitude),
                               (TEST_LATITUDE - 0.001, mid_longitude)]]
        cross = tile._InverseDistanceWeightedDetailed(TEST_LATITUDE, TEST_LONGITUDE,
                                              barriers=cross_tile_barrier)
        self.assertEqual(cross.used_points, 4)
        self.assertEqual(cross.excluded[0].reason, 'blocked_by_barrier[0].segment[2]')

        # Locations without a tile still report as missing, even with barriers set:
        tilemap.set_barriers(local_barrier)
        self.assertIsNone(tilemap._IDW(-47.0, -13.99))
        self.assertIsNone(tilemap._IDW_detailed(-47.0, -13.99))

    def test_batch_mode(self) -> None:
        
        # Two pulls that are far enough apart to require multiple files

        # With batch_mode=False, both files should be kept
        geo_elevation_data = mod_srtm.get_data(batch_mode=False)

        elevation1 = geo_elevation_data.get_elevation(42.3467, 71.0972)
        self.assertTrue(elevation1 > 0) # type: ignore
        self.assertTrue(len(geo_elevation_data.files) == 1)

        elevation2 = geo_elevation_data.get_elevation(43.0382, 87.9298)
        self.assertTrue(elevation2 > 0) # type: ignore
        self.assertTrue(len(geo_elevation_data.files) == 2)

        # With batch_mode=True, only the most recent file should be kept
        geo_elevation_data = mod_srtm.get_data(batch_mode=True)
        elevation1 = geo_elevation_data.get_elevation(42.3467, 71.0972)
        self.assertTrue(elevation1 > 0) # type: ignore
        self.assertTrue(len(geo_elevation_data.files) == 1)
        keys1 = geo_elevation_data.files.keys()

        elevation2 = geo_elevation_data.get_elevation(43.0382, 87.9298)
        self.assertTrue(len(geo_elevation_data.files) == 1)
        self.assertFalse(geo_elevation_data.files.keys() == keys1)


if __name__ == '__main__':
    mod_unittest.main()
