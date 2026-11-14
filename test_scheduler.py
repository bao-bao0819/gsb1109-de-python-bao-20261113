# -*- coding: utf-8 -*-

"""
Tests for the minimum-tile-load scheduler and the survey batch runner.

Run with:
    python -m unittest test_scheduler
"""

import os        as mod_os
import random    as mod_random
import shutil    as mod_shutil
import tempfile  as mod_tempfile
import unittest  as mod_unittest

from srtm import scheduler as mod_scheduler

import survey_batch as mod_batch


class SchedulerTests(mod_unittest.TestCase):

    def test_single_line_min_loads(self) -> None:
        A, B = frozenset({'A'}), frozenset({'B'})
        AB = frozenset({'A', 'B'})
        self.assertEqual(1, mod_scheduler.single_line_min_loads([A, A, A]))
        self.assertEqual(3, mod_scheduler.single_line_min_loads([A, B, A]))
        self.assertEqual(1, mod_scheduler.single_line_min_loads([A, AB, A]))
        self.assertEqual(2, mod_scheduler.single_line_min_loads([A, AB, B]))

    def test_interleaving_saves_loads(self) -> None:
        # Line 1 forces A-B-A (3 loads); line 2 (B, A) embeds for free.
        lines = [[frozenset({'A'}), frozenset({'B'}), frozenset({'A'})],
                 [frozenset({'B'}), frozenset({'A'})]]
        sequence, _ = mod_scheduler.optimal_schedule(lines)
        self.assertEqual(3, len(sequence))

    def test_flexible_edge_point_saves_a_swap(self) -> None:
        # The middle point of line 1 accepts both tiles: one load suffices
        # for the whole batch.  Pinned to B it would force A-B-A.
        flexible = [[frozenset({'A'}), frozenset({'A', 'B'}), frozenset({'A'})]]
        pinned = [[frozenset({'A'}), frozenset({'B'}), frozenset({'A'})]]
        self.assertEqual(1, len(mod_scheduler.optimal_schedule(flexible)[0]))
        self.assertEqual(3, len(mod_scheduler.optimal_schedule(pinned)[0]))

    def test_optimal_matches_brute_force_on_random_instances(self) -> None:
        rng = mod_random.Random(20261113)
        for _ in range(60):
            tiles = ['A', 'B', 'C']
            lines = []
            for _ in range(rng.randint(1, 3)):
                line = []
                for _ in range(rng.randint(1, 5)):
                    line.append(frozenset(rng.sample(tiles, rng.randint(1, 2))))
                lines.append(line)
            sequence, states = mod_scheduler.optimal_schedule(lines)
            brute_loads, _, _ = mod_scheduler.brute_force_min_loads(lines, tiles)
            self.assertEqual(brute_loads, len(sequence))
            # The returned schedule must actually read everything in order.
            self.assertIsNotNone(mod_scheduler.simulate(lines, sequence))
            self.assertEqual(len(states), len(sequence))

    def test_lower_bound_is_admissible(self) -> None:
        rng = mod_random.Random(7)
        for _ in range(40):
            tiles = ['A', 'B']
            lines = [[frozenset(rng.sample(tiles, rng.randint(1, 2)))
                      for _ in range(rng.randint(1, 6))]
                     for _ in range(rng.randint(1, 3))]
            sequence, _ = mod_scheduler.optimal_schedule(lines)
            self.assertLessEqual(mod_scheduler.lower_bound_loads(lines), len(sequence))


class EdgeCandidateTests(mod_unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.workdir = mod_tempfile.mkdtemp(prefix='srtm_checklist_')
        mod_batch._build_checklist_tiles(
            mod_os.path.join(cls.workdir, mod_batch.CHECKLIST_TILE_DIR))
        cls.store = mod_batch.TileStore(
            mod_os.path.join(cls.workdir, mod_batch.CHECKLIST_TILE_DIR))

    @classmethod
    def tearDownClass(cls) -> None:
        mod_shutil.rmtree(cls.workdir)

    def test_interior_point_single_candidate(self) -> None:
        resolved = mod_batch.resolve_point(10.5, 10.5, self.store)
        self.assertEqual(['N10E010.hgt'], resolved['candidates'])
        self.assertEqual(140, resolved['elevation'])

    def test_shared_edge_point_two_candidates(self) -> None:
        resolved = mod_batch.resolve_point(10.0, 11.0, self.store)
        self.assertEqual(['N10E010.hgt', 'N10E011.hgt'], resolved['candidates'])
        self.assertEqual(180, resolved['elevation'])
        self.assertEqual('shared-edge', resolved['note'])

    def test_one_sided_void_pins_the_point(self) -> None:
        # lat=10.5 on the shared lon=11 edge is void on the Y side only.
        resolved = mod_batch.resolve_point(10.5, 11.0, self.store)
        self.assertEqual(['N10E010.hgt'], resolved['candidates'])
        self.assertEqual(150, resolved['elevation'])

    def test_void_everywhere_needs_no_load(self) -> None:
        resolved = mod_batch.resolve_point(20.0, 20.0, self.store)
        self.assertEqual([], resolved['candidates'])
        self.assertIsNone(resolved['elevation'])


class BatchEndToEndTests(mod_unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.workdir = mod_tempfile.mkdtemp(prefix='srtm_batch_')
        cls.result = mod_batch.run_batch(cls.workdir)
        cls.checklist = mod_batch.write_checklist(cls.workdir)

    @classmethod
    def tearDownClass(cls) -> None:
        mod_shutil.rmtree(cls.workdir)

    def test_schedule_is_optimal_against_brute_force(self) -> None:
        sched_lines = self.result['sched_lines']
        brute_loads, _, _ = mod_scheduler.brute_force_min_loads(sched_lines)
        self.assertEqual(brute_loads, self.result['loads'])
        self.assertEqual(self.result['loads'] - 1, self.result['swaps'])

    def test_every_line_delivered_in_mileage_order(self) -> None:
        for line in self.result['lines']:
            path = mod_os.path.join(self.workdir, 'output', 'lines',
                                    '%s.csv' % line['id'])
            self.assertTrue(mod_os.path.exists(path))
            with open(path) as handle:
                rows = handle.read().strip().split('\n')[1:]
            self.assertEqual(len(line['points']), len(rows))
            mileages = [float(row.split(',')[0]) for row in rows]
            self.assertEqual(mileages, sorted(mileages))
            # Every scheduled point carries the elevation planned for it.
            for row, point in zip(rows, sorted(line['points'],
                                               key=lambda p: p['mileage'])):
                elevation = row.split(',')[3]
                expected = point['elevation']
                self.assertEqual('' if expected is None else str(expected),
                                 elevation)

    def test_device_readings_match_direct_reads(self) -> None:
        # Re-read every point straight from the store: the single-block
        # execution must deliver identical elevations.
        store = mod_batch.TileStore(mod_os.path.join(self.workdir,
                                                     mod_batch.TILE_DIR))
        for line in self.result['lines']:
            for point in line['points']:
                if not point['candidates']:
                    self.assertIsNone(point['elevation'])
                    continue
                tile = store.load(point['tile'])
                self.assertEqual(point['elevation'],
                                 tile.value_at(point['lat'], point['lon']))

    def test_checklist_states_hand_checkable_optimum(self) -> None:
        with open(self.checklist) as handle:
            content = handle.read()
        self.assertIn('最短可行长度 = 4', content)
        self.assertIn('>= 4', content)


if __name__ == '__main__':
    mod_unittest.main()
