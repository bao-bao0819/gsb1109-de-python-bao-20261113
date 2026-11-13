#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Tests for simultaneous blindness under uncertain outage windows.

Copyright 2013-2018 Chaim Leib Halbert

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
from __future__ import absolute_import

import json
import random

import pytest

from intervaltree import (
    BlindnessEvent,
    IntervalTree,
    simultaneous_blindness_range,
)
from intervaltree.blindness import (
    _intersection_segments,
    _length,
    _merge_segments,
    _shifted,
    format_witness,
    solve_scenario,
)


def _duration(result, kind, groups):
    return result.lower.duration if kind == 'lower' else result.upper.duration


def _brute_force(events_a, events_b, window, groups, mode, minimise,
                 steps=81):
    """Grid search used as an independent oracle in two dimensions."""
    variable = [group for group in groups
                if groups[group][0] != groups[group][1]]
    fixed = dict((group, groups[group][0]) for group in groups
                 if groups[group][0] == groups[group][1])
    if not variable:
        offsets = dict(fixed)
        return _length(_intersection_segments(
            _shifted(events_a, mode, offsets),
            _shifted(events_b, mode, offsets), window))

    axes = []
    for group in variable:
        low, high = groups[group]
        axes.append([low + (high - low) * step / float(steps - 1)
                     for step in range(steps)])

    best = None
    import itertools
    for values in itertools.product(*axes):
        offsets = dict(fixed)
        offsets.update(zip(variable, values))
        value = _length(_intersection_segments(
            _shifted(events_a, mode, offsets),
            _shifted(events_b, mode, offsets), window))
        if best is None:
            best = value
        elif minimise and value < best:
            best = value
        elif not minimise and value > best:
            best = value
    return best


def test_unshared_events_use_cores_for_lower_bound():
    result = simultaneous_blindness_range(
        [(10, 20, 12, 16)],
        [(12, 18, 13, 17)],
        window=(0, 100),
    )
    assert result.minimum == 3
    assert result.maximum == 6
    assert result.lower.overlap == [(13, 16)]
    assert result.upper.overlap == [(12, 18)]


def test_shared_offset_moves_the_whole_group_together():
    events_a = [(10, 20, 12, 16, 'g')]
    events_b = [(12, 18, 13, 17, 'g')]
    result = simultaneous_blindness_range(
        events_a, events_b, window=(0, 100), clock_groups={'g': (-5, 5)})
    # One rigid shift cannot change the two bounds, and both witness
    # intervals must use exactly the same offset.
    assert result.minimum == 3
    assert result.maximum == 6
    assert result.lower.offsets == result.upper.offsets == {'g': 0.0}
    for kind in (result.lower, result.upper):
        shift = kind.offsets['g']
        expected_a = (12 + shift) if kind is result.lower else (10 + shift)
        expected_b = (13 + shift) if kind is result.lower else (12 + shift)
        assert kind.intervals['A'][0][0] == expected_a
        assert kind.intervals['B'][0][0] == expected_b


def test_groups_are_rigid_but_independent_of_each_other():
    events_a = [
        BlindnessEvent(10, 20, 12, 16, group='g1'),
        BlindnessEvent(30, 40, 33, 37, group='g1'),
    ]
    events_b = [BlindnessEvent(15, 25, 17, 23, group='g2')]
    groups = {'g1': (-5, 5), 'g2': (-5, 5)}
    result = simultaneous_blindness_range(
        events_a, events_b, window=(0, 100), clock_groups=groups)

    for kind in (result.lower, result.upper):
        shift = kind.offsets['g1']
        expected = (12, 33) if kind is result.lower else (10, 30)
        assert tuple(begin - shift for begin, end in kind.intervals['A']) == \
            tuple(expected)
        shift_b = kind.offsets['g2']
        expected_b = 17 if kind is result.lower else 15
        for begin, end in kind.intervals['B']:
            assert begin - shift_b == expected_b

    assert result.minimum == pytest.approx(
        _brute_force(events_a, events_b, (0, 100), groups, 'core', True),
        abs=0.2)
    assert result.maximum == pytest.approx(
        _brute_force(events_a, events_b, (0, 100), groups, 'full', False),
        abs=0.2)


def test_a_rigid_group_cannot_be_shifted_per_event():
    # Per-event maxima would total 4 + 4, but a shared skew has a single
    # value, so at most one of the two pairs can be aligned at a time.
    events_a = [(0, 10, 2, 8, 'g'), (20, 30, 22, 28, 'g')]
    events_b = [(3, 7, 4, 6), (40, 44, 41, 43)]
    result = simultaneous_blindness_range(
        events_a, events_b, window=(-50, 50), clock_groups={'g': (-6, 6)})
    assert result.maximum == 4
    assert result.upper.offsets == {'g': 0.0}


def test_empty_core_is_a_point_of_certainty():
    # The upper bound still uses the full envelopes, so it stays 6; the
    # point-like core contributes zero to the lower bound.
    result = simultaneous_blindness_range(
        [(10, 20, 15, 15)], [(12, 18, 13, 17)], window=(0, 100))
    assert result.minimum == 0
    assert result.maximum == 6


def test_query_window_is_applied_after_shifting():
    scenario = {
        'window': [15, 18],
        'clock_groups': {'g': (-100, 100)},
        'links': {
            'north': [{'earliest_start': 0, 'latest_end': 20,
                       'core': [4, 10], 'group': 'g'}],
            'south': [{'earliest_start': 0, 'latest_end': 20,
                       'core': [6, 16], 'group': 'g'}],
        },
    }
    result = solve_scenario(scenario)
    assert result.minimum == 0
    assert result.maximum == 3
    for kind in (result.lower, result.upper):
        for begin, end in kind.overlap:
            assert 15 <= begin and end <= 18


def test_unbounded_group_still_has_a_finite_witness():
    scenario = {
        'window': [0, 100],
        'clock_groups': {'g': [None, None]},
        'links': {
            'north': [{'earliest_start': 10, 'latest_end': 20,
                       'core': [12, 16], 'group': 'g'}],
            'south': [{'earliest_start': 12, 'latest_end': 18,
                       'core': [13, 17], 'group': 'g'}],
        },
    }
    result = solve_scenario(scenario)
    assert result.minimum == 0
    assert result.maximum == 6
    assert isinstance(result.upper.offsets['g'], float)


def test_witness_segments_add_up_to_the_stated_bound():
    random.seed(20261113)
    groups = {'g': (-6, 6), 'h': (-3, 3)}
    for trial in range(12):
        events_a = []
        events_b = []
        for target, count in ((events_a, 3), (events_b, 3)):
            for index in range(count):
                begin = random.randint(-5, 20)
                end = begin + random.randint(2, 15)
                core_begin = random.randint(begin, end)
                core_end = random.randint(core_begin, end)
                target.append(BlindnessEvent(
                    begin, end, core_begin, core_end,
                    group=random.choice(['g', 'h', None])))
        result = simultaneous_blindness_range(
            events_a, events_b, window=(0, 30), clock_groups=groups)
        for kind in (result.lower, result.upper):
            assert _length(kind.overlap) == pytest.approx(
                kind.duration, abs=1e-9)
            assert _length(kind.overlap) == pytest.approx(
                _length(_intersection_segments(
                    kind.intervals['A'], kind.intervals['B'],
                    result.window)), abs=1e-9)


def test_witness_interval_count_matches_the_events():
    scenario = {
        'window': [0, 100],
        'clock_groups': {'g1': (-4, 4), 'g2': (-2, 2)},
        'links': {
            'north': [
                {'earliest_start': 0, 'latest_end': 10, 'core': [2, 6],
                 'group': 'g1'},
                {'earliest_start': 20, 'latest_end': 40, 'core': [24, 30],
                 'group': 'g1'},
            ],
            'south': [
                {'earliest_start': 5, 'latest_end': 25, 'core': [8, 16],
                 'group': 'g2'},
            ],
        },
    }
    result = solve_scenario(scenario)
    assert len(result.lower.intervals['north']) == 2
    assert len(result.lower.intervals['south']) == 1
    assert result.lower.coverage['north'] == _merge_segments(
        result.lower.intervals['north'], result.window)


def test_rejects_a_core_outside_its_envelope():
    with pytest.raises(ValueError):
        BlindnessEvent(10, 20, 5, 15)
    with pytest.raises(ValueError):
        BlindnessEvent(10, 20, 15, 25)
    with pytest.raises(ValueError):
        BlindnessEvent(20, 10, 12, 16)


def test_rejects_offset_bounds_without_a_group():
    with pytest.raises(ValueError):
        BlindnessEvent(0, 10, 2, 8, offset_bounds=(-1, 1))


def test_contradictory_inline_bounds_are_rejected():
    events = [
        {'earliest_start': 0, 'latest_end': 10, 'core': [2, 8],
         'group': 'g', 'offset_bounds': [-5, -2]},
        {'earliest_start': 0, 'latest_end': 10, 'core': [2, 8],
         'group': 'g', 'offset_bounds': [1, 3]},
    ]
    with pytest.raises(ValueError):
        simultaneous_blindness_range(events, events, window=(0, 10))


def test_single_link_still_reports_zero_overlap():
    result = simultaneous_blindness_range(
        [(0, 10, 2, 8)], [], window=(0, 10), clock_groups={})
    assert result.minimum == 0
    assert result.maximum == 0


def test_flat_event_list_is_grouped_by_link():
    scenario = {
        'window': [0, 20],
        'events': [
            {'link': 'north', 'earliest_start': 0, 'latest_end': 10,
             'core': [2, 8]},
            {'link': 'south', 'earliest_start': 4, 'latest_end': 12,
             'core': [6, 10]},
        ],
    }
    result = solve_scenario(scenario)
    assert result.minimum == 2
    assert result.upper.duration == 6


def test_json_round_trip_and_text_witness():
    scenario = {
        'window': [0, 100],
        'clock_groups': {'g': (-5, 5)},
        'links': {
            'north': [{'earliest_start': 10, 'latest_end': 20,
                       'core_start': 12, 'core_end': 16, 'group': 'g'}],
            'south': [{'earliest_start': 12, 'latest_end': 18,
                       'core_start': 13, 'core_end': 17, 'group': 'g'}],
        },
    }
    result = solve_scenario(json.loads(json.dumps(scenario)))
    data = result.to_dict()
    assert data['minimum'] == 3
    assert data['maximum'] == 6
    assert data['lower']['overlap'] == [[13.0, 16.0]]
    text = format_witness(result)
    assert 'lower bound: 3.0' in text
    assert 'upper bound: 6.0' in text
    assert 'total: 3.0' in text


def test_existing_interval_tree_api_is_untouched():
    tree = IntervalTree.from_tuples([(1, 2), (4, 7), (5, 9)])
    assert sorted(tree)[0].begin == 1
    assert len(tree.overlap(2, 5)) == 1
