# -*- coding: utf-8 -*-

"""
Tests for srtm.clearance.

Run with:
    python -m unittest test_clearance
"""

import csv
import math
import os
import struct
import tempfile
import unittest
from typing import Dict, List, Optional, Set, Tuple

from srtm import data as mod_data
from srtm import utils as mod_utils
from srtm import clearance as mod_clearance


def make_tile(side: int = 11,
              elevations: Optional[Dict[Tuple[int, int], float]] = None,
              voids: Optional[Set[Tuple[int, int]]] = None,
              name: str = 'N00E000.hgt') -> mod_data.GeoElevationFile:
    elevations = elevations or {}
    voids = voids or set()
    values = []
    for row in range(side):
        for column in range(side):
            if (row, column) in voids:
                values.append(-32768)
            else:
                values.append(int(elevations.get((row, column), 100)))
    raw = struct.pack('>%dh' % len(values), *values)
    tile_map = mod_data.GeoElevationData(
        {}, {}, file_handler=mod_utils.FileHandler(tempfile.mkdtemp()))
    return mod_data.GeoElevationFile(name, raw, tile_map)


def station(mileage: float, latitude: float, longitude: float = 0.5,
            wire: float = 120.0, sigma: float = 0.1) -> mod_clearance.Station:
    return mod_clearance.Station(mileage, latitude, longitude, wire, sigma)


class IdwTests(unittest.TestCase):

    def test_matches_package_idw_on_real_tile(self) -> None:
        with open(os.path.join('test_files', 'N44W072.hgt'), 'rb') as handle:
            raw = handle.read()
        tile_map = mod_data.GeoElevationData(
            {}, {}, file_handler=mod_utils.FileHandler(tempfile.mkdtemp()))
        tile = mod_data.GeoElevationFile('N44W072.hgt', raw, tile_map)
        # Awkward decimals avoid grid-node alignment, where the package's
        # own IDW raises ZeroDivisionError (documented and handled in
        # idw_ground).
        for latitude, longitude in [
                (44.1756325, -71.5965699),
                (44.3131313, -71.2727272),
                (44.8080808, -71.9191919),
                (44.5001, -71.5001),
                (44.33333, -71.77777)]:
            expected = tile._InverseDistanceWeighted(latitude, longitude)
            elevation, _, valid, _, _ = mod_clearance.idw_ground(
                tile, latitude, longitude, raster_sigma=0.0)
            assert elevation is not None
            assert expected is not None
            self.assertGreaterEqual(valid, 2)
            self.assertAlmostEqual(elevation, expected, places=9)

    def test_exact_cell_centre_returns_raw_value(self) -> None:
        tile = make_tile(elevations={(5, 5): 142})
        elevation, sigma, valid, _, _ = mod_clearance.idw_ground(
            tile, 0.5, 0.5, raster_sigma=1.5)
        assert sigma is not None
        self.assertEqual(elevation, 142.0)
        self.assertAlmostEqual(sigma, 1.5)
        self.assertEqual(valid, 1)

    def test_void_cells_are_never_replaced_by_zero(self) -> None:
        # Point (0.52, 0.52) sits in cell (row=4, column=5); its radius-1
        # cross is (3,5) north, (4,4) west, (4,5) centre, (4,6) east,
        # (5,5) south.  Void the whole cross.
        voids = {(3, 5), (4, 4), (4, 5), (4, 6), (5, 5)}
        tile = make_tile(voids=voids)
        elevation, sigma, valid, void_count, _ = mod_clearance.idw_ground(
            tile, 0.52, 0.52, min_support=2)
        self.assertIsNone(elevation)
        self.assertIsNone(sigma)
        self.assertEqual(valid, 0)
        self.assertEqual(void_count, 5)

        # With one surviving neighbour the IDW uses that value, not zero.
        tile = make_tile(elevations={(4, 5): 130},
                         voids={(3, 5), (4, 4), (4, 6), (5, 5)})
        elevation, _, valid, void_count, _ = mod_clearance.idw_ground(
            tile, 0.52, 0.52, min_support=1)
        self.assertEqual(valid, 1)
        self.assertEqual(void_count, 4)
        self.assertEqual(elevation, 130.0)

    def test_raster_error_propagates_through_weights(self) -> None:
        tile = make_tile(elevations={(5, 5): 100, (5, 6): 200})
        elevation, sigma, _, _, _ = mod_clearance.idw_ground(
            tile, 0.5, 0.55, raster_sigma=4.0)
        # Weighted value lies between the two valid elevations.
        assert elevation is not None
        assert sigma is not None
        self.assertGreater(elevation, 100.0)
        self.assertLess(elevation, 200.0)
        self.assertGreater(sigma, 0.0)
        self.assertLess(sigma, 4.0)


class WireInterpolationTests(unittest.TestCase):

    def setUp(self) -> None:
        self.stations = [
            station(0.0, 0.1, wire=100.0, sigma=0.3),
            station(100.0, 0.9, wire=120.0, sigma=0.5),
        ]

    def test_rss_mode(self) -> None:
        elevation, sigma = mod_clearance.interpolate_wire(
            self.stations, 50.0, mode='rss')
        self.assertAlmostEqual(elevation, 110.0)
        self.assertAlmostEqual(sigma, math.hypot(0.15, 0.25))

    def test_envelope_mode(self) -> None:
        elevation, sigma = mod_clearance.interpolate_wire(
            self.stations, 50.0, mode='envelope')
        self.assertAlmostEqual(elevation, 110.0)
        self.assertAlmostEqual(sigma, 0.4)

    def test_outside_range_clamps_to_endpoint(self) -> None:
        elevation, sigma = mod_clearance.interpolate_wire(
            self.stations, -50.0)
        self.assertEqual((elevation, sigma), (100.0, 0.3))


class ClassificationTests(unittest.TestCase):

    def test_thresholds(self) -> None:
        self.assertEqual(mod_clearance.classify(4.0, 6.0, 8.0),
                         mod_clearance.DEFICIENT)
        self.assertEqual(mod_clearance.classify(9.0, 11.0, 8.0),
                         mod_clearance.SUFFICIENT)
        self.assertEqual(mod_clearance.classify(7.0, 9.0, 8.0),
                         mod_clearance.POSSIBLE)
        self.assertEqual(mod_clearance.classify(None, None, 8.0),
                         mod_clearance.UNKNOWN)


class EndToEndTests(unittest.TestCase):

    def setUp(self) -> None:
        # Void patch big enough to void every cross cell around its centre.
        voids = {(row, column) for row in range(3, 7)
                 for column in range(4, 7)}
        self.tile = make_tile(voids=voids)
        self.stations = [
            station(0.0, 0.10, wire=105.0),
            station(100.0, 0.26, wire=105.0),
            station(200.0, 0.42, wire=120.0),
            station(300.0, 0.58, wire=120.0),
            station(400.0, 0.74, wire=120.0),
            station(500.0, 0.90, wire=105.0),
        ]

    def test_segments_contiguous_and_statuses_present(self) -> None:
        result = mod_clearance.analyze_clearance(
            self.stations, self.tile, required_clearance=8.0,
            confidence=1.96, raster_sigma=0.0, step=4.0)
        statuses = {segment.status for segment in result.segments}
        self.assertIn(mod_clearance.DEFICIENT, statuses)
        self.assertIn(mod_clearance.SUFFICIENT, statuses)
        self.assertIn(mod_clearance.UNKNOWN, statuses)

        # Segments cover every evaluated sample; adjacent segments are
        # separated by exactly one sample step, the change-point bracket.
        self.assertEqual(result.segments[0].start_mileage, 0.0)
        self.assertEqual(result.segments[-1].end_mileage, 500.0)
        for earlier, later in zip(result.segments, result.segments[1:]):
            self.assertAlmostEqual(
                later.start_mileage - earlier.end_mileage, result.step)

        # Every inter-segment gap appears as a reported transition.
        self.assertEqual(len(result.transitions), len(result.segments) - 1)
        for transition, earlier in zip(result.transitions,
                                       result.segments[:-1]):
            self.assertEqual(transition.mileage_left, earlier.end_mileage)

        # No evaluation ever treats the void sentinel as an elevation.
        for sample in result.samples:
            if sample.ground_elevation is not None:
                self.assertAlmostEqual(sample.ground_elevation, 100.0)

    def test_transition_brackets_and_actions(self) -> None:
        result = mod_clearance.analyze_clearance(
            self.stations, self.tile, required_clearance=8.0,
            confidence=1.96, raster_sigma=0.0, step=4.0)
        self.assertTrue(result.transitions)
        for transition in result.transitions:
            self.assertEqual(
                transition.position_half_width,
                (transition.mileage_right - transition.mileage_left) / 2.0)
            involves_void = mod_clearance.UNKNOWN in (
                transition.from_status, transition.to_status)
            if involves_void:
                self.assertIn('复测', transition.action)

    def test_combined_sigma_and_bands(self) -> None:
        stations = [
            mod_clearance.Station(0.0, 0.2, 0.5, 110.0, 0.3),
            mod_clearance.Station(100.0, 0.8, 0.5, 110.0, 0.5),
        ]
        result = mod_clearance.analyze_clearance(
            stations, make_tile(), required_clearance=8.0,
            confidence=2.0, raster_sigma=2.0, step=50.0)
        midpoint = next(sample for sample in result.samples
                        if math.isclose(sample.mileage, 50.0))
        expected_wire_sigma = math.hypot(0.15, 0.25)
        expected_sigma = math.hypot(expected_wire_sigma, 2.0)
        assert midpoint.clearance is not None
        assert midpoint.clearance_sigma is not None
        assert midpoint.clearance_low is not None
        assert midpoint.clearance_high is not None
        self.assertAlmostEqual(midpoint.wire_sigma, expected_wire_sigma)
        self.assertAlmostEqual(midpoint.clearance_sigma, expected_sigma)
        self.assertAlmostEqual(
            midpoint.clearance_low,
            midpoint.clearance - 2.0 * expected_sigma)
        self.assertAlmostEqual(
            midpoint.clearance_high,
            midpoint.clearance + 2.0 * expected_sigma)

    def test_worst_point_can_fall_between_stations(self) -> None:
        # Gaussian hill whose peak sits between two sparse stations.
        side = 51

        def elevation(row: int, column: int) -> float:
            latitude = 1.0 - row / (side - 1)
            distance = (latitude - 0.5) / 0.03
            return 100.0 + 25.0 * math.exp(-0.5 * distance * distance)

        elevations = {(row, column): elevation(row, column)
                      for row in range(side) for column in range(side)}
        tile = make_tile(side=side, elevations=elevations,
                         name='N00E000.hgt')
        stations = [
            mod_clearance.Station(0.0, 0.2, 0.5, 118.0, 0.1),
            mod_clearance.Station(400.0, 0.8, 0.5, 118.0, 0.1),
        ]
        result = mod_clearance.analyze_clearance(
            stations, tile, required_clearance=8.0, confidence=1.96,
            raster_sigma=0.5, step=5.0)
        dangerous = [segment for segment in result.segments
                     if segment.status != mod_clearance.SUFFICIENT
                     and segment.status != mod_clearance.UNKNOWN]
        self.assertTrue(dangerous)
        worst_mileage = min(segment.worst_mileage for segment in dangerous)
        self.assertGreater(worst_mileage, 0.0)
        self.assertLess(worst_mileage, 400.0)


class CsvLoadingTests(unittest.TestCase):

    def test_load_and_validation(self) -> None:
        handle = tempfile.NamedTemporaryFile(
            mode='w', suffix='.csv', delete=False, newline='')
        with handle:
            writer = csv.DictWriter(handle, fieldnames=[
                'mileage', 'latitude', 'longitude',
                'wire_elevation', 'wire_sigma'])
            writer.writeheader()
            writer.writerow({'mileage': 100, 'latitude': 0.9, 'longitude': 0.5,
                             'wire_elevation': 120, 'wire_sigma': 0.2})
            writer.writerow({'mileage': 0, 'latitude': 0.1, 'longitude': 0.5,
                             'wire_elevation': 110, 'wire_sigma': 0.3})
        try:
            stations = mod_clearance.load_stations(handle.name)
            self.assertEqual([s.mileage for s in stations], [0.0, 100.0])
            self.assertEqual(stations[0].wire_sigma, 0.3)
        finally:
            os.unlink(handle.name)


if __name__ == '__main__':
    unittest.main()
