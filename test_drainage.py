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
Offline tests for the multi-outlet drainage analysis.

Run with:
    $ python -m unittest test_drainage
"""

import unittest as mod_unittest
from typing import List, Optional, Sequence, Tuple

from srtm import data as mod_data
from srtm import drainage as mod_drainage
from srtm import utils as mod_utils

Connectivity = mod_drainage.Connectivity
CellKind = mod_drainage.CellKind


def _is_connected_neighbor(
    first: Tuple[int, int], second: Tuple[int, int], connectivity: Connectivity
) -> bool:
    row_delta = abs(first[0] - second[0])
    column_delta = abs(first[1] - second[1])
    if connectivity == Connectivity.FOUR:
        return row_delta + column_delta == 1
    return max(row_delta, column_delta) == 1


class DrainageTests(mod_unittest.TestCase):

    def assert_path_valid(
        self,
        grid: Sequence[Sequence[Optional[float]]],
        result: mod_drainage.DrainageResult,
        start: Tuple[int, int],
        outlet_id: int,
    ) -> List[Tuple[int, int]]:
        outlet_path = result.path(start[0], start[1], outlet_id)
        self.assertIsNotNone(outlet_path)
        assert outlet_path is not None  # for mypy
        self.assertEqual(outlet_path[0], start)
        self.assertEqual(outlet_path[-1], result.outlets[outlet_id])
        other_outlets = {
            position for other_id, position in result.outlets.items() if other_id != outlet_id
        }
        for previous, current in zip(outlet_path, outlet_path[1:]):
            self.assertTrue(_is_connected_neighbor(previous, current, result.connectivity))
            previous_elevation = grid[previous[0]][previous[1]]
            current_elevation = grid[current[0]][current[1]]
            assert previous_elevation is not None and current_elevation is not None
            self.assertGreaterEqual(previous_elevation, current_elevation)
            self.assertNotIn(current, other_outlets)
        return outlet_path

    def test_flat_cell_with_two_downhill_edges_is_disputed(self) -> None:
        # Every low cell is on one connected contour surface and has a valid
        # move towards both numbered outlets; a single-neighbour assignment
        # would depend on traversal order, the label set must not.
        grid = [
            [2, 1, 2],
            [1, 1, 1],
            [2, 1, 2],
        ]
        outlets = {1: (0, 1), 2: (2, 1)}
        result = mod_drainage.analyze_drainage(grid, outlets, Connectivity.FOUR)

        # An outlet is absorbing and only carries its own label.
        self.assertEqual(result.outlets_for(0, 1), frozenset({1}))
        self.assertEqual(result.outlets_for(2, 1), frozenset({2}))

        disputed = [
            (row, column)
            for row in range(3)
            for column in range(3)
            if (row, column) not in {(0, 1), (2, 1)}
        ]
        for row, column in disputed:
            self.assertEqual(result.outlets_for(row, column), frozenset({1, 2}))
            self.assertTrue(result.is_disputed(row, column))
            self.assertEqual(result.kind(row, column), CellKind.DRAINED)

        self.assertEqual(sorted(result.disputed_cells()), sorted(disputed))
        self.assert_path_valid(grid, result, (2, 2), 1)
        self.assert_path_valid(grid, result, (2, 2), 2)

    def test_closed_depression_is_separate_from_void_leak(self) -> None:
        grid = [
            [5, 5, 5, 5, 5],
            [5, 1, 1, 1, 5],
            [5, 1, 0, 1, 5],
            [5, 1, 1, 1, 5],
            [5, 5, 5, 5, 5],
        ]
        result = mod_drainage.analyze_drainage(grid, {7: (0, 4)}, Connectivity.FOUR)

        # The outer flat 5-ring drains to the single outlet ...
        self.assertEqual(result.outlets_for(0, 0), frozenset({7}))
        self.assertFalse(result.is_disputed(0, 0))
        # ... while the low basin has no valid flat-or-downhill way out.
        for row in range(1, 4):
            for column in range(1, 4):
                self.assertEqual(result.outlets_for(row, column), frozenset())
                self.assertTrue(result.is_closed_depression(row, column))
                self.assertFalse(result.can_leak_to_void(row, column))
                self.assertEqual(result.kind(row, column), CellKind.CLOSED_DEPRESSION)

        self.assertEqual(
            sorted(result.closed_depression_cells()),
            sorted((row, column) for row in range(1, 4) for column in range(1, 4)),
        )
        self.assertEqual(result.void_leak_cells(), [])
        self.assertIsNone(result.path(2, 2, 7))

    def test_void_wall_splits_outlets_and_marks_possible_leaks(self) -> None:
        # Elevations increase away from the top corners, so each side drains
        # monotonically towards its own outlet and cannot cross the void wall.
        grid = [
            [1, 2, None, 2, 1],
            [2, 3, None, 3, 2],
            [3, 4, None, 4, 3],
            [4, 5, None, 5, 4],
            [5, 6, None, 6, 5],
        ]
        result = mod_drainage.analyze_drainage(grid, {1: (0, 0), 2: (0, 4)}, Connectivity.FOUR)

        for row in range(5):
            self.assertTrue(result.is_void(row, 2))
            self.assertEqual(result.kind(row, 2), CellKind.VOID)
            for column in (0, 1):
                self.assertEqual(result.outlets_for(row, column), frozenset({1}))
            for column in (3, 4):
                self.assertEqual(result.outlets_for(row, column), frozenset({2}))

        # Cells neighbouring the unknown area may spill into it even though
        # they also have a numbered outlet; the leak summary must include them.
        leak_cells = set(result.void_leak_cells())
        for row in range(5):
            self.assertTrue(result.can_leak_to_void(row, 1))
            self.assertTrue(result.can_leak_to_void(row, 3))
            self.assertIn((row, 1), leak_cells)
            self.assertIn((row, 3), leak_cells)
        self.assertFalse(result.can_leak_to_void(0, 0))
        self.assertEqual(result.closed_depression_cells(), [])

    def test_leak_into_void_without_outlets(self) -> None:
        grid = [
            [5, 5, 5, None],
            [5, 1, 1, None],
            [5, 1, 0, None],
            [5, 5, 5, None],
        ]
        result = mod_drainage.analyze_drainage(grid, {}, Connectivity.FOUR)

        known_cells = [
            (row, column)
            for row in range(4)
            for column in range(4)
            if not result.is_void(row, column)
        ]
        self.assertTrue(known_cells)
        for row, column in known_cells:
            self.assertTrue(result.can_leak_to_void(row, column))
            self.assertEqual(result.kind(row, column), CellKind.VOID_LEAK)
        self.assertEqual(
            sorted(result.void_leak_cells()),
            sorted(known_cells),
        )

    def test_depression_and_leak_coexist(self) -> None:
        # The 0/1 basin is enclosed by 9s and cannot reach the void column;
        # the 9 cells touch the void and are possible leaks.
        grid = [
            [1, 1, 9, None],
            [1, 0, 9, None],
            [9, 9, 9, None],
        ]
        result = mod_drainage.analyze_drainage(grid, {}, Connectivity.FOUR)

        basin = [(0, 0), (0, 1), (1, 0), (1, 1)]
        for cell in basin:
            self.assertTrue(result.is_closed_depression(*cell))
            self.assertFalse(result.can_leak_to_void(*cell))
        for cell in [(0, 2), (1, 2), (2, 0), (2, 1), (2, 2)]:
            self.assertFalse(result.is_closed_depression(*cell))
            self.assertTrue(result.can_leak_to_void(*cell))
            self.assertEqual(result.kind(*cell), CellKind.VOID_LEAK)

    def test_four_vs_eight_connectivity_through_diagonal_void_gap(self) -> None:
        grid = [
            [1, None, 2],
            [None, 5, None],
            [3, None, 4],
        ]
        outlets = {1: (0, 0), 2: (0, 2), 3: (2, 0), 4: (2, 2)}

        four = mod_drainage.analyze_drainage(grid, outlets, Connectivity.FOUR)
        self.assertEqual(four.outlets_for(1, 1), frozenset())
        self.assertTrue(four.can_leak_to_void(1, 1))
        self.assertEqual(four.kind(1, 1), CellKind.VOID_LEAK)

        eight = mod_drainage.analyze_drainage(grid, outlets, Connectivity.EIGHT)
        self.assertEqual(eight.outlets_for(1, 1), frozenset({1, 2, 3, 4}))
        self.assertTrue(eight.is_disputed(1, 1))
        for outlet_id in outlets:
            self.assert_path_valid(grid, eight, (1, 1), outlet_id)

    def test_result_is_independent_of_outlet_and_grid_traversal_order(self) -> None:
        grid = [
            [5, 5, 5, 5, 5],
            [5, 1, 1, 1, 5],
            [5, 1, 0, 1, 5],
            [5, 1, 1, 1, 5],
            [5, 5, 5, 5, 5],
        ]
        first = mod_drainage.analyze_drainage(grid, {1: (0, 0), 2: (4, 4)}, Connectivity.EIGHT)
        second = mod_drainage.analyze_drainage(grid, {2: (4, 4), 1: (0, 0)}, Connectivity.EIGHT)

        for row in range(5):
            for column in range(5):
                self.assertEqual(
                    first.outlets_for(row, column),
                    second.outlets_for(row, column),
                )
                self.assertEqual(
                    first.can_leak_to_void(row, column),
                    second.can_leak_to_void(row, column),
                )

    def test_paths_are_non_increasing_and_outlets_absorb(self) -> None:
        # A monotone slope towards the two top corners: the central ridge can
        # bend towards either outlet, while every move stays flat-or-downhill.
        grid = [
            [1, 1, 2, 1, 1],
            [2, 2, 3, 2, 2],
            [3, 3, 4, 3, 3],
            [4, 4, 5, 4, 4],
            [5, 5, 6, 5, 5],
        ]
        outlets = {1: (0, 0), 2: (0, 4)}
        result = mod_drainage.analyze_drainage(grid, outlets, Connectivity.FOUR)

        self.assertEqual(result.outlets_for(2, 2), frozenset({1, 2}))
        paths = result.paths(2, 2)
        self.assertEqual(set(paths.keys()), {1, 2})
        for outlet_id, outlet_path in paths.items():
            self.assertEqual(outlet_path[0], (2, 2))
            self.assertEqual(outlet_path[-1], result.outlets[outlet_id])
            for previous, current in zip(outlet_path, outlet_path[1:]):
                self.assertTrue(
                    _is_connected_neighbor(previous, current, Connectivity.FOUR)
                )
                self.assertGreaterEqual(
                    grid[previous[0]][previous[1]], grid[current[0]][current[1]]
                )

    def test_outlet_cell_only_carries_its_own_label(self) -> None:
        grid = [
            [1, 1, 1],
            [1, 1, 1],
            [1, 1, 1],
        ]
        result = mod_drainage.analyze_drainage(grid, {1: (0, 0), 2: (2, 2)}, Connectivity.FOUR)
        self.assertEqual(result.outlets_for(0, 0), frozenset({1}))
        self.assertEqual(result.outlets_for(2, 2), frozenset({2}))
        self.assertTrue(result.is_disputed(1, 1))
        self.assertEqual(result.path(0, 0, 1), [(0, 0)])
        self.assertIsNone(result.path(0, 0, 2))

    def test_invalid_arguments(self) -> None:
        grid: List[List[Optional[float]]] = [[1, None], [2, 3]]

        with self.assertRaises(ValueError):
            mod_drainage.analyze_drainage(grid, {1: (0, 1)}, Connectivity.FOUR)
        with self.assertRaises(ValueError):
            mod_drainage.analyze_drainage(grid, {1: (5, 5)}, Connectivity.FOUR)
        with self.assertRaises(ValueError):
            mod_drainage.analyze_drainage(grid, {1: (0, 0), 2: (0, 0)}, Connectivity.FOUR)
        empty_grid: List[List[Optional[float]]] = [[]]
        with self.assertRaises(ValueError):
            mod_drainage.analyze_drainage(empty_grid, {1: (0, 0)}, Connectivity.FOUR)
        with self.assertRaises(ValueError):
            mod_drainage.analyze_drainage([[1, 2], [3]], {1: (0, 0)}, Connectivity.FOUR)
        with self.assertRaises(TypeError):
            mod_drainage.analyze_drainage(grid, {1: (0, 0)}, connectivity=8)  # type: ignore

        result = mod_drainage.analyze_drainage(grid, {1: (0, 0)}, Connectivity.FOUR)
        with self.assertRaises(ValueError):
            result.outlets_for(9, 9)
        with self.assertRaises(KeyError):
            result.path(1, 0, 99)
        with self.assertRaises(ValueError):
            result.path(0, 1, 1)

    def test_srtm_void_marker_is_treated_as_void(self) -> None:
        marker = mod_drainage.VOID_MARKER
        grid = [
            [2, marker],
            [1, 2],
        ]
        result = mod_drainage.analyze_drainage(grid, {1: (1, 0)}, Connectivity.FOUR)
        self.assertTrue(result.is_void(0, 1))
        self.assertTrue(result.can_leak_to_void(0, 0))
        self.assertEqual(result.outlets_for(0, 0), frozenset({1}))

    def test_grid_from_real_tile_smoke_test(self) -> None:
        with open("test_files/N44W072.hgt", "rb") as hgt_file:
            hgt = hgt_file.read()
        tilemap = mod_data.GeoElevationData({}, {}, file_handler=mod_utils.FileHandler(""))
        tile = mod_data.GeoElevationFile("N44W072.hgt", hgt, tilemap)

        full_grid = mod_drainage.grid_from_tile(tile)
        self.assertEqual(len(full_grid), tile.square_side)
        self.assertEqual(len(full_grid[0]), tile.square_side)

        # Work on a small crop so the test stays fast and fully offline.
        crop = [row[:40] for row in full_grid[:40]]
        outlets = {1: (0, 0), 2: (39, 39)}
        if crop[0][0] is None:
            outlets.pop(1)
        if crop[39][39] is None:
            outlets.pop(2)
        self.assertTrue(outlets)

        result = mod_drainage.analyze_drainage(crop, outlets, Connectivity.EIGHT)
        classified = 0
        for row in range(40):
            for column in range(40):
                if result.is_void(row, column):
                    continue
                classified += 1
                self.assertIn(
                    result.kind(row, column),
                    {
                        CellKind.DRAINED,
                        CellKind.CLOSED_DEPRESSION,
                        CellKind.VOID_LEAK,
                    },
                )
        self.assertGreater(classified, 0)

        # Re-running with reversed outlet order must give identical labels.
        reversed_outlets = {outlet_id: position for outlet_id, position in reversed(list(outlets.items()))}
        rerun = mod_drainage.analyze_drainage(crop, reversed_outlets, Connectivity.EIGHT)
        for row in range(40):
            for column in range(40):
                self.assertEqual(
                    result.outlets_for(row, column),
                    rerun.outlets_for(row, column),
                )


if __name__ == "__main__":
    mod_unittest.main()
