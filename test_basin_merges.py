# -*- coding: utf-8 -*-

"""
Tests for the depression merge analysis in :mod:`srtm.basin_merges`.

Run with::

    python -m unittest test_basin_merges
    # or
    pytest test_basin_merges.py
"""

import os
import random
import struct
import tempfile
import unittest

from srtm.basin_merges import (
    BasinMergeTable,
    build_basin_merge_table,
    build_basin_merge_table_from_cells,
    build_from_hgt_file,
    load_basin_merge_table,
    read_hgt_grid,
)


def rotate90(grid, side):
    return [grid[(side - 1 - column) * side + row]
            for row in range(side) for column in range(side)]


def reflect_rows(grid, side):
    return [grid[(side - 1 - row) * side + column]
            for row in range(side) for column in range(side)]


def neighbours(index, side):
    row, column = divmod(index, side)
    if row > 0:
        yield index - side
    if row + 1 < side:
        yield index + side
    if column > 0:
        yield index - 1
    if column + 1 < side:
        yield index + 1


def is_border(index, side):
    row, column = divmod(index, side)
    return row == 0 or column == 0 or row == side - 1 or column == side - 1


def flood_components(grid, side, level):
    seen = set()
    for index, value in enumerate(grid):
        if value is None or value > level or index in seen:
            continue
        stack = [index]
        seen.add(index)
        cells = set()
        border = False
        while stack:
            current = stack.pop()
            cells.add(current)
            border = border or is_border(current, side)
            for neighbour in neighbours(current, side):
                if (neighbour not in seen
                        and grid[neighbour] is not None
                        and grid[neighbour] <= level):
                    seen.add(neighbour)
                    stack.append(neighbour)
        yield frozenset(cells), border


def geometric_oracle(grid, side):
    """
    Independent ground truth built only from threshold flood components.

    Returns a set of ``(level, member minimum-plateau cell sets, flooded size)``
    tuples with no reliance on the implementation under test.
    """
    levels = sorted(set(value for value in grid if value is not None))
    depressions = []
    for level in levels:
        for cells, border in flood_components(grid, side, level):
            if border:
                continue
            if not all(grid[index] == level for index in cells):
                continue
            if any(cells & existing for existing in depressions):
                continue
            depressions.append(cells)

    first_seen = {}
    for level in levels:
        for cells, border in flood_components(grid, side, level):
            if border:
                continue
            members = frozenset(dep for dep in depressions if dep <= cells)
            if len(members) >= 2 and members not in first_seen:
                first_seen[members] = (level, len(cells))
    return {(level, members, size) for members, (level, size) in first_seen.items()}


def builder_geometric_events(table):
    return {
        (
            event.level,
            frozenset(frozenset(table.basin_birth_cells[label])
                      for label in event.members),
            event.flooded_cells,
        )
        for event in table.merge_events
    }


def transformed_geometric_events(table, side, cell_transform):
    """Geometric events with every cell index passed through ``cell_transform``."""
    return {
        (
            event.level,
            frozenset(frozenset(cell_transform(index)
                                for index in table.basin_birth_cells[label])
                      for label in event.members),
            event.flooded_cells,
        )
        for event in table.merge_events
    }


def rotate_index(index, side):
    row, column = divmod(index, side)
    return column * side + (side - 1 - row)


def reflect_index(index, side):
    row, column = divmod(index, side)
    return (side - 1 - row) * side + column


class GridLoadingTests(unittest.TestCase):

    def test_read_hgt_grid_decodes_signed_shorts_and_keeps_voids(self):
        values = [0, 1, -5, 10000, -1000, 10001, -32768, 32767, 7]
        data = struct.pack('>%dh' % len(values), *values)
        grid, side = read_hgt_grid(data)
        self.assertEqual(side, 3)
        self.assertEqual(grid, [0, 1, -5, 10000, -1000, None, None, None, 7])

    def test_non_square_grid_raises(self):
        with self.assertRaises(ValueError):
            read_hgt_grid(struct.pack('>3h', 1, 2, 3))


class MergeLogicTests(unittest.TestCase):

    def test_two_depressions_connect_at_saddle(self):
        # Two low pockets joined by a single saddle cell at height 5.
        grid = [
            9, 9, 9, 9, 9,
            9, 1, 5, 2, 9,
            9, 9, 9, 9, 9,
            9, 9, 9, 9, 9,
            9, 9, 9, 9, 9,
        ]
        table = build_basin_merge_table(grid, 5, cell_area=2.0)
        self.assertEqual(len(table.merge_events), 1)
        event = table.merge_events[0]
        self.assertEqual(event.level, 5)
        self.assertEqual(event.members, (0, 1))
        self.assertEqual(event.saddle, (1, 2))
        self.assertEqual(event.flooded_cells, 3)
        self.assertEqual(table.connection_level(0, 1), 5)
        answer = table.basin_for_query([0, 1])
        self.assertEqual(answer['level'], 5)
        self.assertEqual(answer['flooded_cells'], 3)
        self.assertEqual(answer['flooded_area'], 6.0)
        self.assertEqual(table.events_at_or_below(4), [])
        self.assertEqual(len(table.events_at_or_below(5)), 1)

    def test_no_data_wall_blocks_connection(self):
        # A full top-to-bottom void wall splits the grid into two halves.
        grid = [
            9, 9, None, 9, 9,
            9, 1, None, 2, 9,
            9, 9, None, 9, 9,
            9, 9, None, 9, 9,
            9, 9, None, 9, 9,
        ]
        table = build_basin_merge_table(grid, 5)
        self.assertEqual(table.merge_events, [])
        self.assertIsNone(table.connection_level(0, 1))
        self.assertIsNone(table.basin_for_query([0, 1]))
        # Each depression spills independently over its own border.
        self.assertEqual(len(table.border_spills), 2)

    def test_three_way_saddle_is_one_merge(self):
        # Three pockets all meet one connected saddle plateau at height 5.
        grid = [
            9, 9, 9, 9, 9,
            9, 1, 5, 2, 9,
            9, 5, 5, 5, 9,
            9, 9, 3, 9, 9,
            9, 9, 9, 9, 9,
        ]
        table = build_basin_merge_table(grid, 5)
        self.assertEqual(len(table.merge_events), 1)
        event = table.merge_events[0]
        self.assertEqual(event.level, 5)
        self.assertEqual(event.members, (0, 1, 2))
        self.assertEqual(table.connection_level(0, 2), 5)
        self.assertEqual(table.connection_level(1, 2), 5)

    def test_staged_pair_then_third_basin(self):
        grid = [
            9, 9, 9, 9, 9,
            9, 1, 4, 2, 9,
            9, 9, 6, 9, 9,
            9, 9, 3, 9, 9,
            9, 9, 9, 9, 9,
        ]
        table = build_basin_merge_table(grid, 5)
        levels = [(event.level, event.members) for event in table.merge_events]
        self.assertEqual(levels, [(4, (0, 1)), (6, (0, 1, 2))])
        self.assertEqual(table.connection_level(0, 1), 4)
        self.assertEqual(table.connection_level(0, 2), 6)
        self.assertEqual(table.connection_level(1, 2), 6)

    def test_uphill_extension_does_not_emit_duplicate_merge(self):
        # Once 0 and 1 merge at 5, higher cells extending the same basin must
        # not create additional merge events for the same member set.
        grid = [
            9, 9, 9, 9, 9,
            9, 1, 5, 2, 9,
            9, 9, 7, 9, 9,
            9, 9, 8, 9, 9,
            9, 9, 9, 9, 9,
        ]
        table = build_basin_merge_table(grid, 5)
        self.assertEqual([event.members for event in table.merge_events],
                         [(0, 1)])


class InvarianceTests(unittest.TestCase):

    def test_reading_order_does_not_change_result(self):
        grid = [
            9, 9, None, 9, 9,
            9, 1, None, 2, 9,
            9, 9, None, 9, 9,
            9, 9, 5, 9, 9,
            9, 9, None, 9, 9,
        ]
        side = 5
        reference = build_basin_merge_table(grid, side)
        cells = [(i, v) for i, v in enumerate(grid) if v is not None]
        shuffled = cells[:]
        random.Random(42).shuffle(shuffled)
        shuffled_table = build_basin_merge_table_from_cells(shuffled, side)
        reversed_table = build_basin_merge_table_from_cells(
            list(reversed(cells)), side)
        expected = builder_geometric_events(reference)
        self.assertEqual(builder_geometric_events(shuffled_table), expected)
        self.assertEqual(builder_geometric_events(reversed_table), expected)

    def test_rotation_and_reflection_preserve_merges(self):
        rng = random.Random(20261114)
        for _ in range(40):
            side = rng.randint(4, 8)
            grid = [rng.choice([None, None, 0, 1, 2, 3, 5, 9])
                    for _ in range(side * side)]
            reference = build_basin_merge_table(grid, side)

            rotated = grid
            transform = lambda index: index
            for _ in range(4):
                rotated = rotate90(rotated, side)
                transform = (lambda previous, n=side:
                             lambda index: rotate_index(previous(index), n))(transform)
                expected = transformed_geometric_events(reference, side, transform)
                self.assertEqual(
                    builder_geometric_events(build_basin_merge_table(rotated, side)),
                    expected)

            reflected = reflect_rows(grid, side)
            expected = transformed_geometric_events(
                reference, side, lambda index: reflect_index(index, side))
            self.assertEqual(
                builder_geometric_events(build_basin_merge_table(reflected, side)),
                expected)

    def test_matches_independent_flood_oracle(self):
        rng = random.Random(7)
        for _ in range(60):
            side = rng.randint(4, 8)
            grid = [rng.choice([None, None, 0, 1, 2, 4, 8])
                    for _ in range(side * side)]
            table = build_basin_merge_table(grid, side)
            self.assertEqual(builder_geometric_events(table),
                             geometric_oracle(grid, side))


class PersistenceTests(unittest.TestCase):

    def setUp(self):
        self.grid = [
            9, 9, 9, 9, 9,
            9, 1, 5, 2, 9,
            9, 5, 5, 5, 9,
            9, 9, 3, 9, 9,
            9, 9, 9, 9, 9,
        ]
        self.table = build_basin_merge_table(
            self.grid, 5, file_name='sample.hgt', cell_area=4.0)

    def test_json_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'merges.json')
            self.table.save(path)
            reloaded = load_basin_merge_table(path)
            self.assertEqual(reloaded.file_name, 'sample.hgt')
            self.assertEqual(reloaded.side, 5)
            self.assertEqual(reloaded.cell_area, 4.0)
            self.assertEqual(
                [(e.level, e.members, e.saddle, e.flooded_cells)
                 for e in reloaded.merge_events],
                [(e.level, e.members, e.saddle, e.flooded_cells)
                 for e in self.table.merge_events])
            self.assertEqual(reloaded.basin_birth_cells,
                             self.table.basin_birth_cells)
            self.assertEqual(reloaded.connection_level(0, 2), 5)
            self.assertEqual(reloaded.basin_for_query([0, 1, 2])['flooded_area'],
                             reloaded.merge_events[0].flooded_cells * 4.0)

    def test_csv_export_has_one_row_per_merge(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'merges.csv')
            self.table.save_csv(path)
            with open(path, encoding='utf-8') as stream:
                rows = [line.strip() for line in stream if line.strip()]
        self.assertEqual(rows[0].split(',')[:2], ['level', 'members'])
        self.assertEqual(len(rows) - 1, len(self.table.merge_events))
        self.assertIn('0|1|2', rows[1])

    def test_build_from_hgt_file(self):
        values = list(range(1, 10))  # 3x3, all valid
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'N00E000.hgt')
            with open(path, 'wb') as stream:
                stream.write(struct.pack('>9h', *values))
            table = build_from_hgt_file(path)
        self.assertIsInstance(table, BasinMergeTable)
        self.assertEqual(table.file_name, 'N00E000.hgt')
        self.assertEqual(table.side, 3)


if __name__ == '__main__':
    unittest.main()
