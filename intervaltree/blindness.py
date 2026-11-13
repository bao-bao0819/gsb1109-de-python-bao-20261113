#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
intervaltree: simultaneous blindness under uncertain outage windows.

Every recorded outage supplies three facts:

* ``earliest_start`` - the outage cannot have started before this time;
* ``latest_end`` - the outage cannot have ended after this time;
* a *core* segment that was certainly blind.

The real outage is therefore any interval that contains the core and lies
inside the earliest/latest envelope.  Recorders that share a clock skew form
a group: choosing an offset for that group moves every event in the group by
exactly the same amount.  Events from different groups move independently.
Events without a group are used as recorded.

This module answers the operations question: over a query window, how short
and how long can the period be during which two collection links are blind
at the same time?  Both bounds are returned with a witness - one concrete
offset per clock group plus the concrete intervals - so a duty operator can
add the witness segments up on a timeline and recover the two numbers.

Only the intersection with the query window is measured.  Parts of an outage
outside the window are ignored.

The bounds are exact for the model above.  The joint-blind duration is a
continuous piecewise-linear function of the group offsets; its extrema over
the offset box occur at vertices of the arrangement induced by the interval
endpoints and the box faces.  Those vertices are enumerated and evaluated.

Python 2/3 compatible; no third-party dependencies.
"""
from __future__ import absolute_import

import itertools
import json
import sys

__all__ = [
    'BlindnessEvent',
    'BlindnessBound',
    'BlindnessResult',
    'simultaneous_blindness_range',
    'simultaneous_blindness_bounds',
    'simultaneous_outage_range',
    'possible_blindness_range',
    'blindness_range',
    'solve',
    'solve_scenario',
    'format_witness',
]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _first_key(mapping, names, default=None):
    for name in names:
        if name in mapping:
            return mapping[name]
    return default


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require_number(value, what):
    if not _is_number(value):
        raise TypeError("%s must be a number, got %r" % (what, value))
    return value


def _coerce_pair(value, what):
    if _is_number(value):
        return value, value
    if isinstance(value, (tuple, list)) and len(value) == 2:
        low, high = value
    elif isinstance(value, dict):
        low = _first_key(value, (
            'min', 'low', 'lo', 'lower', 'from', 'begin', 'start',
            'min_offset', 'offset_min', 'skew_min'))
        high = _first_key(value, (
            'max', 'high', 'hi', 'upper', 'to', 'end', 'finish',
            'max_offset', 'offset_max', 'skew_max'))
    else:
        raise TypeError("%s must be a number or a pair, got %r" %
                        (what, value))
    low = _require_number(low, what + ' lower bound')
    high = _require_number(high, what + ' upper bound')
    if high < low:
        raise ValueError("%s has reversed bounds: %r" % (what, value))
    return low, high


def _coerce_group_pair(value, what):
    """Like _coerce_pair, but ``None`` marks a bound at infinity.

    Infinite groups are normalised to a finite box just large enough that
    every meaningful alignment is inside it; see ``_finite_box``.
    """
    if value is None:
        return None, None
    if _is_number(value):
        return value, value
    if isinstance(value, (tuple, list)) and len(value) == 2:
        low, high = value
    elif isinstance(value, dict):
        low = _first_key(value, (
            'min', 'low', 'lo', 'lower', 'from', 'begin', 'start',
            'min_offset', 'offset_min', 'skew_min'))
        high = _first_key(value, (
            'max', 'high', 'hi', 'upper', 'to', 'end', 'finish',
            'max_offset', 'offset_max', 'skew_max'))
    else:
        raise TypeError("%s must be a number, a pair, or None, got %r" %
                        (what, value))
    if low is not None:
        low = _require_number(low, what + ' lower bound')
    if high is not None:
        high = _require_number(high, what + ' upper bound')
    if low is not None and high is not None and high < low:
        raise ValueError("%s has reversed bounds: %r" % (what, value))
    return low, high


def _intersect_ranges(ranges, what):
    if not ranges:
        return None
    lows = [item[0] for item in ranges if item[0] is not None]
    highs = [item[1] for item in ranges if item[1] is not None]
    low = max(lows) if lows else None
    high = min(highs) if highs else None
    if low is not None and high is not None and high < low:
        raise ValueError(
            "%s has contradictory bounds: %r" % (what, ranges))
    return low, high


def _finite_box(all_events, window, groups):
    """Turn possibly unbounded group boxes into a finite, sufficient box.

    All arrangement breakpoints lie between the extreme endpoint and window
    differences, so one margin beyond that range captures every extremum.
    A witness may therefore use a finite offset even for an unbounded group.
    """
    coordinates = []
    for event in all_events:
        coordinates.extend(event.interval('full'))
        coordinates.extend(event.interval('core'))
    if window is not None:
        coordinates.extend(window)
    low_coord = min(coordinates)
    high_coord = max(coordinates)
    span = high_coord - low_coord
    margin = span * 0.25 + 1.0
    break_low = low_coord - high_coord
    break_high = high_coord - low_coord

    variable_groups = []
    fixed = {}
    lows = []
    highs = []
    resolved = {}
    for group in sorted(groups, key=lambda value: str(value)):
        given_low, given_high = groups[group]
        low = break_low - margin if given_low is None else given_low
        high = break_high + margin if given_high is None else given_high
        if given_low is not None and given_high is None:
            high = max(high, low + margin)
        if given_high is not None and given_low is None:
            low = min(low, high - margin)
        resolved[group] = (low, high)
        if high == low:
            fixed[group] = low
        else:
            variable_groups.append(group)
            lows.append(low)
            highs.append(high)
    return resolved, fixed, variable_groups, lows, highs


# ---------------------------------------------------------------------------
# Input coercion
# ---------------------------------------------------------------------------

class BlindnessEvent(object):
    """One recorded outage with uncertain ends and a certain core."""

    __slots__ = (
        'earliest_start', 'latest_end', 'core_start', 'core_end',
        'group', 'label', 'inline_offset_bounds',
    )

    def __init__(self, earliest_start, latest_end, core_start, core_end,
                 group=None, label=None, offset_bounds=None):
        self.earliest_start = _require_number(earliest_start, 'earliest start')
        self.latest_end = _require_number(latest_end, 'latest end')
        self.core_start = _require_number(core_start, 'core start')
        self.core_end = _require_number(core_end, 'core end')
        if self.latest_end < self.earliest_start:
            raise ValueError(
                "outage %r ends before it starts: [%r, %r]" %
                (label, self.earliest_start, self.latest_end))
        if not (self.earliest_start <= self.core_start <= self.core_end <=
                self.latest_end):
            raise ValueError(
                "outage %r has a core outside its earliest/latest envelope: "
                "envelope [%r, %r], core [%r, %r]" %
                (label, self.earliest_start, self.latest_end,
                 self.core_start, self.core_end))
        self.group = group
        self.label = label
        if offset_bounds is not None and group is None:
            raise ValueError(
                "event %r declares offset bounds but no clock group" %
                (label,))
        self.inline_offset_bounds = (
            None if offset_bounds is None
            else _coerce_pair(offset_bounds, 'offset bounds of %r' % (label,))
        )

    def interval(self, mode):
        """Return ``(begin, end)`` for mode ``'full'`` or ``'core'``."""
        if mode == 'core':
            return self.core_start, self.core_end
        if mode == 'full':
            return self.earliest_start, self.latest_end
        raise ValueError("unknown interval mode: %r" % (mode,))

    def shifted(self, offset, mode):
        begin, end = self.interval(mode)
        return begin + offset, end + offset

    def __repr__(self):
        return (
            "BlindnessEvent(earliest_start=%r, latest_end=%r, "
            "core_start=%r, core_end=%r, group=%r, label=%r)" %
            (self.earliest_start, self.latest_end, self.core_start,
             self.core_end, self.group, self.label))


def _coerce_core(value, label):
    if value is None:
        raise ValueError("event %r is missing its definite core segment" %
                         (label,))
    if isinstance(value, dict):
        begin = _first_key(value, ('start', 'begin', 'from', 'core_start'))
        end = _first_key(value, ('end', 'finish', 'to', 'core_end'))
    elif isinstance(value, (tuple, list)) and len(value) == 2:
        begin, end = value
    else:
        raise TypeError("event %r has an invalid core segment: %r" %
                        (label, value))
    begin = _require_number(begin, 'core start of %r' % (label,))
    end = _require_number(end, 'core end of %r' % (label,))
    if end < begin:
        raise ValueError("event %r has a reversed core segment: %r" %
                         (label, value))
    return begin, end


def _event_from_sequence(values, index, link):
    if len(values) == 4:
        earliest, latest, core_start, core_end = values
        group = None
    elif len(values) == 5:
        earliest, latest, core_start, core_end, group = values
    else:
        raise TypeError(
            "event tuple must have 4 or 5 items, got %r" % (values,))
    return BlindnessEvent(
        earliest, latest, core_start, core_end,
        group=group, label='%s-event-%d' % (link, index),
    )


def _event_from_mapping(obj, index, link):
    label = _first_key(obj, ('label', 'id', 'name', 'alarm', 'event'),
                       '%s-event-%d' % (link, index))
    earliest = _first_key(obj, (
        'earliest_start', 'earliest_begin', 'earliest', 'min_start',
        'start_min', 'start_earliest', 'lower_start', 'begin_earliest',
        'start'))
    latest = _first_key(obj, (
        'latest_end', 'latest_finish', 'latest', 'max_end', 'end_max',
        'end_latest', 'upper_end', 'finish_latest', 'end'))
    if earliest is None or latest is None:
        raise ValueError("event %r needs earliest_start and latest_end" %
                         (label,))
    core = _first_key(obj, (
        'core', 'core_segment', 'definite', 'certain', 'certain_segment'))
    if core is None:
        core_start = _first_key(obj, ('core_start', 'definite_start',
                                      'certain_start'))
        core_end = _first_key(obj, ('core_end', 'definite_end',
                                    'certain_end'))
        if core_start is None or core_end is None:
            raise ValueError("event %r needs core or core_start/core_end" %
                             (label,))
        core = (core_start, core_end)
    group = _first_key(obj, (
        'group', 'clock_group', 'offset_group', 'skew_group',
        'clock', 'skew', 'clock_id'))
    offset_bounds = _first_key(obj, (
        'offset_bounds', 'clock_offset_bounds', 'skew_bounds',
        'offset_range', 'shift_bounds'))
    if offset_bounds is None:
        offset_low = _first_key(obj, (
            'min_offset', 'offset_min', 'skew_min', 'clock_offset_min',
            'min_shift', 'shift_min'))
        offset_high = _first_key(obj, (
            'max_offset', 'offset_max', 'skew_max', 'clock_offset_max',
            'max_shift', 'shift_max'))
        if offset_low is not None or offset_high is not None:
            if offset_low is None or offset_high is None:
                raise ValueError(
                    "event %r needs both min_offset and max_offset" % (label,))
            offset_bounds = (offset_low, offset_high)
        else:
            fixed = _first_key(obj, (
                'offset', 'clock_offset', 'skew', 'clock_skew', 'shift'))
            if fixed is not None:
                offset_bounds = (fixed, fixed)
    core_start, core_end = _coerce_core(core, label)
    return BlindnessEvent(
        earliest, latest, core_start, core_end,
        group=group, label=label, offset_bounds=offset_bounds,
    )


def _coerce_event(obj, index, link):
    if isinstance(obj, BlindnessEvent):
        return obj
    if isinstance(obj, dict):
        return _event_from_mapping(obj, index, link)
    if isinstance(obj, (tuple, list)):
        return _event_from_sequence(obj, index, link)
    needed = ('earliest_start', 'latest_end', 'core_start', 'core_end')
    if all(hasattr(obj, name) for name in needed):
        return BlindnessEvent(
            getattr(obj, 'earliest_start'), getattr(obj, 'latest_end'),
            getattr(obj, 'core_start'), getattr(obj, 'core_end'),
            group=getattr(obj, 'group', None),
            label=getattr(obj, 'label', '%s-event-%d' % (link, index)),
            offset_bounds=getattr(obj, 'offset_bounds', None),
        )
    raise TypeError("cannot read outage event %r" % (obj,))


def _coerce_events(events, link):
    if events is None:
        raise ValueError("missing outage events for link %r" % (link,))
    if isinstance(events, dict):
        events = _first_key(events, ('events', 'alarms', 'outages', 'items'))
        if events is None:
            raise ValueError("link %r has no events" % (link,))
    return [_coerce_event(event, index, link)
            for index, event in enumerate(events)]


def _coerce_window(window):
    if window is None:
        return None
    if isinstance(window, dict):
        begin = _first_key(window, ('begin', 'start', 'from', 'window_start'))
        end = _first_key(window, ('end', 'finish', 'to', 'window_end'))
        if begin is None or end is None:
            inner = _first_key(window, ('window', 'query_window', 'range'))
            if inner is None:
                raise ValueError("query window needs begin and end")
            return _coerce_window(inner)
        window = (begin, end)
    if not isinstance(window, (tuple, list)) or len(window) != 2:
        raise TypeError("query window must be a pair, got %r" % (window,))
    begin = _require_number(window[0], 'query window start')
    end = _require_number(window[1], 'query window end')
    if end <= begin:
        raise ValueError("query window is empty: %r" % (window,))
    return begin, end


def _coerce_groups(groups):
    if groups is None:
        return {}
    if not isinstance(groups, dict):
        raise TypeError("clock_groups must be a mapping")
    return dict(
        (label, _coerce_group_pair(bounds, 'clock group %r' % (label,)))
        for label, bounds in groups.items())


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

class BlindnessBound(object):
    """One bound together with a complete, self-consistent witness.

    Attributes:
        kind: ``'lower'`` or ``'upper'``.
        duration: total joint-blind duration inside the query window.
        offsets: chosen offset for every clock group.
        intervals: per link, recorded-plus-offset intervals used here.
        coverage: per link, merged blind coverage inside the window.
        overlap: merged joint-blind segments inside the window.
    """

    __slots__ = ('kind', 'duration', 'offsets', 'intervals', 'coverage',
                 'overlap', 'window')

    def __init__(self, kind, duration, offsets, intervals, coverage, overlap,
                 window):
        self.kind = kind
        self.duration = duration
        self.offsets = offsets
        self.intervals = intervals
        self.coverage = coverage
        self.overlap = overlap
        self.window = window

    def to_dict(self):
        return {
            'kind': self.kind,
            'duration': self.duration,
            'offsets': dict(self.offsets),
            'intervals': dict(
                (name, [list(segment) for segment in segments])
                for name, segments in self.intervals.items()),
            'coverage': dict(
                (name, [list(segment) for segment in segments])
                for name, segments in self.coverage.items()),
            'overlap': [list(segment) for segment in self.overlap],
            'window': list(self.window) if self.window is not None else None,
        }

    as_dict = to_dict

    # Convenient aliases, since operations tooling tends to use either word.
    @property
    def shifts(self):
        return self.offsets

    @property
    def blind_segments(self):
        return self.overlap

    def __getitem__(self, key):
        if key in ('duration', 'length', 'value', 'total'):
            return self.duration
        if key in ('offsets', 'clock_offsets', 'skews', 'shifts'):
            return self.offsets
        if key in ('intervals', 'links'):
            return self.intervals
        if key in ('coverage', 'covered'):
            return self.coverage
        if key in ('overlap', 'segments', 'blind_segments'):
            return self.overlap
        if key in ('kind', 'bound'):
            return self.kind
        if key in ('window', 'query_window'):
            return self.window
        raise KeyError(key)

    def __iter__(self):
        for segment in self.overlap:
            yield segment

    def __repr__(self):
        return ("BlindnessBound(kind=%r, duration=%r, offsets=%r, "
                "overlap=%r)") % (
                    self.kind, self.duration, self.offsets, self.overlap)


class BlindnessResult(object):
    """Lower and upper possible values of simultaneous blindness."""

    __slots__ = ('lower', 'upper', 'window', 'links')

    def __init__(self, lower, upper, window, links):
        self.lower = lower
        self.upper = upper
        self.window = window
        self.links = links

    @property
    def minimum(self):
        return self.lower.duration

    @property
    def maximum(self):
        return self.upper.duration

    min_duration = minimum
    max_duration = maximum

    @property
    def bounds(self):
        return self.minimum, self.maximum

    def as_tuple(self):
        return self.minimum, self.maximum

    def to_dict(self):
        return {
            'minimum': self.minimum,
            'maximum': self.maximum,
            'lower': self.lower.to_dict(),
            'upper': self.upper.to_dict(),
            'window': list(self.window) if self.window is not None else None,
            'links': list(self.links),
        }

    as_dict = to_dict

    def __getitem__(self, key):
        if key in ('lower', 'low', 0):
            return self.lower
        if key in ('upper', 'high', 1):
            return self.upper
        if key in ('window', 'query_window', 'range'):
            return self.window
        if key in ('links', 'paths', 'chains'):
            return self.links
        if key in ('minimum', 'min', 'min_duration', 'lo_duration'):
            return self.minimum
        if key in ('maximum', 'max', 'max_duration', 'hi_duration'):
            return self.maximum
        raise KeyError(key)

    def __iter__(self):
        yield self.lower
        yield self.upper

    def __repr__(self):
        return "BlindnessResult(minimum=%r, maximum=%r)" % (
            self.minimum, self.maximum)


# ---------------------------------------------------------------------------
# Interval geometry
# ---------------------------------------------------------------------------

def _clip(begin, end, window):
    if window is not None:
        begin = max(begin, window[0])
        end = min(end, window[1])
    if end <= begin:
        return None
    return begin, end


def _merge_segments(segments, window=None):
    """Merge half-open segments; optionally clip them to the window."""
    prepared = []
    for begin, end in segments:
        if end < begin:
            raise ValueError("reversed interval: [%r, %r)" % (begin, end))
        if end == begin:
            # An empty core is legal input (a point of certainty) but
            # contributes nothing to any union.
            continue
        piece = _clip(begin, end, window)
        if piece is not None:
            prepared.append(piece)
    if not prepared:
        return []
    prepared.sort()
    result = [prepared[0]]
    for begin, end in prepared[1:]:
        last_begin, last_end = result[-1]
        if begin <= last_end:
            if end > last_end:
                result[-1] = (last_begin, end)
        else:
            result.append((begin, end))
    return result


def _intersection_segments(left_segments, right_segments, window=None):
    """Merged intersection of two unions of half-open segments."""
    left = _merge_segments(left_segments, window)
    right = _merge_segments(right_segments, window)
    if not left or not right:
        return []

    events = []
    for begin, end in left:
        events.append((begin, 1, 0))
        events.append((end, -1, 0))
    for begin, end in right:
        events.append((begin, 0, 1))
        events.append((end, 0, -1))
    events.sort()

    result = []
    count_left = count_right = 0
    previous = events[0][0]
    for point, delta_left, delta_right in events:
        if point > previous and count_left > 0 and count_right > 0:
            if result and result[-1][1] == previous:
                result[-1] = (result[-1][0], point)
            else:
                result.append((previous, point))
        count_left += delta_left
        count_right += delta_right
        previous = point
    return result


def _length(segments):
    return sum(end - begin for begin, end in segments)


def _shifted(events, mode, offsets):
    result = []
    for event in events:
        result.append(event.shifted(offsets.get(event.group, 0), mode))
    return result


# ---------------------------------------------------------------------------
# Exact optimisation over rigid offset groups
# ---------------------------------------------------------------------------

def _plane_feasible(coeff, rhs, lows, highs):
    minimum = maximum = 0.0
    for value, low, high in zip(coeff, lows, highs):
        if value > 0:
            minimum += value * low
            maximum += value * high
        elif value < 0:
            minimum += value * high
            maximum += value * low
    tolerance = 1e-9 * (1.0 + abs(rhs))
    return minimum - tolerance <= rhs <= maximum + tolerance


def _solve_square(planes):
    """Solve a square linear system; return None if it is singular."""
    size = len(planes)
    matrix = [[float(value) for value in coeff] + [float(rhs)]
              for coeff, rhs in planes]
    for column in range(size):
        pivot = max(range(column, size),
                    key=lambda row: abs(matrix[row][column]))
        if abs(matrix[pivot][column]) < 1e-12:
            return None
        if pivot != column:
            matrix[column], matrix[pivot] = matrix[pivot], matrix[column]
        divisor = matrix[column][column]
        for index in range(column, size + 1):
            matrix[column][index] /= divisor
        for row in range(size):
            if row == column:
                continue
            factor = matrix[row][column]
            if factor == 0:
                continue
            for index in range(column, size + 1):
                matrix[row][index] -= factor * matrix[column][index]
    return [matrix[row][size] for row in range(size)]


def _arrangement_planes(events_a, events_b, fixed, index, lows, highs,
                        window, mode):
    """Hyperplanes where the piecewise-linear duration can change slope.

    Every pair of endpoints is included: even two ends of the same link can
    exchange the order in which they bound that link's union, and that also
    changes the slope of the joint-blind length.
    """
    dimension = len(index)
    planes = {}

    def add(coeff, rhs):
        key = (tuple(coeff), rhs)
        if key not in planes:
            planes[key] = (list(coeff), rhs)

    def endpoint(event, which):
        begin, end = event.interval(mode)
        base = begin if which == 'begin' else end
        group = event.group
        if group is not None and group in index:
            return base, index[group]
        if group is not None:
            base += fixed.get(group, 0)
        return base, None

    endpoints_a = [(event, which) for event in events_a
                   for which in ('begin', 'end')]
    endpoints_b = [(event, which) for event in events_b
                   for which in ('begin', 'end')]

    endpoints = endpoints_a + endpoints_b
    for position, (event_a, which_a) in enumerate(endpoints):
        base_a, var_a = endpoint(event_a, which_a)
        for event_b, which_b in endpoints[position + 1:]:
            base_b, var_b = endpoint(event_b, which_b)
            if var_a is None and var_b is None:
                continue
            coeff = [0.0] * dimension
            if var_a is not None:
                coeff[var_a] += 1.0
            if var_b is not None:
                coeff[var_b] -= 1.0
            if not any(coeff):
                continue
            rhs = base_b - base_a
            if _plane_feasible(coeff, rhs, lows, highs):
                add(coeff, rhs)

    window_points = [] if window is None else [window[0], window[1]]
    for event, which in endpoints_a + endpoints_b:
        base, var = endpoint(event, which)
        if var is None:
            continue
        for point in window_points:
            coeff = [0.0] * dimension
            coeff[var] = 1.0
            rhs = point - base
            if _plane_feasible(coeff, rhs, lows, highs):
                add(coeff, rhs)

    for position in range(dimension):
        for bound in (lows[position], highs[position]):
            coeff = [0.0] * dimension
            coeff[position] = 1.0
            add(coeff, bound)

    return list(planes.values())


MAX_VERTEX_COMBINATIONS = 3000000


def _vertices(plane_list, dimension, lows, highs):
    """Yield every vertex of the box cut by the arrangement planes.

    A vertex is the intersection of ``dimension`` independent planes.  The
    combination count is checked up front: if it is too large, raise rather
    than silently returning an approximate answer.
    """
    if dimension == 0:
        yield tuple()
        return

    combination_count = 1
    for step in range(dimension):
        combination_count *= (len(plane_list) - step)
    for divisor in range(1, dimension + 1):
        combination_count //= divisor

    if combination_count > MAX_VERTEX_COMBINATIONS:
        raise ValueError(
            "too many independent clock groups for an exact answer "
            "(%d candidate vertices); reduce the number of unbounded "
            "groups or tighten their offset ranges" % combination_count)

    for chosen in itertools.combinations(plane_list, dimension):
        point = _solve_square(chosen)
        if point is None:
            continue
        if any(value < low - 1e-9 or value > high + 1e-9
               for value, low, high in zip(point, lows, highs)):
            continue
        yield tuple(min(max(value, low), high)
                    for value, low, high in zip(point, lows, highs))


def _best_offsets(events_a, events_b, fixed, variable_groups, lows, highs,
                  window, mode, minimise):
    """Find an offset assignment minimising/maximising joint blindness."""
    dimension = len(variable_groups)
    index = dict((group, position)
                 for position, group in enumerate(variable_groups))
    plane_list = _arrangement_planes(
        events_a, events_b, fixed, index, lows, highs, window, mode)

    midpoint = dict(
        (group, (lows[index[group]] + highs[index[group]]) / 2.0)
        for group in variable_groups)

    state = {
        'value': None, 'offsets': None, 'segments': None, 'coverage': None,
    }

    def consider(values):
        offsets = dict(fixed)
        for group, value in zip(variable_groups, values):
            offsets[group] = value
        intervals_a = _shifted(events_a, mode, offsets)
        intervals_b = _shifted(events_b, mode, offsets)
        segments = _intersection_segments(intervals_a, intervals_b, window)
        value = _length(segments)
        if state['value'] is None:
            replace = True
        elif minimise:
            replace = value < state['value'] - 1e-12
        else:
            replace = value > state['value'] + 1e-12
        if not replace and abs(value - state['value']) <= 1e-12:
            distance = sum(abs(offsets[group] - midpoint[group])
                           for group in variable_groups)
            best_distance = sum(
                abs(state['offsets'][group] - midpoint[group])
                for group in variable_groups)
            replace = distance < best_distance
        if replace:
            state['value'] = value
            state['offsets'] = offsets
            state['segments'] = segments
            state['coverage'] = (
                _merge_segments(intervals_a, window),
                _merge_segments(intervals_b, window),
            )

    if dimension == 0:
        consider(tuple())
    else:
        for values in _vertices(plane_list, dimension, lows, highs):
            consider(values)
        # The median point is often the most readable witness when several
        # assignments tie for the optimum.
        consider(tuple(midpoint[group] for group in variable_groups))

    if state['offsets'] is None:
        # Only possible if the vertex enumerator produced nothing at all.
        consider(tuple(midpoint[group] for group in variable_groups))

    return (state['value'], state['offsets'], state['segments'],
            state['coverage'])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _scenario_paths(scenario):
    window = _coerce_window(_first_key(
        scenario, ('window', 'query_window', 'range')))
    groups = _coerce_groups(_first_key(
        scenario, ('clock_groups', 'offset_groups', 'groups', 'offsets',
                   'skews', 'clock_skews')))

    for key in ('links', 'paths', 'chains', 'lanes'):
        if key in scenario:
            mapping = scenario[key]
            if not isinstance(mapping, dict):
                raise TypeError("%s must map names to event lists" % (key,))
            if len(mapping) != 2:
                raise ValueError("exactly two links are required")
            return list(mapping.items()), window, groups

    for left, right in (('a', 'b'), ('A', 'B'), ('left', 'right'),
                        ('link_a', 'link_b'), ('path_a', 'path_b')):
        if left in scenario and right in scenario:
            return ([(left, scenario[left]), (right, scenario[right])],
                    window, groups)

    flat = _first_key(scenario, ('events', 'alarms', 'outages', 'records'))
    if flat is not None:
        grouped = {}
        for event in flat:
            name = _first_key(event, (
                'link', 'path', 'chain', 'lane', 'collector'))
            if name is None:
                raise ValueError(
                    "flat events need a link/path/chain/lane field")
            grouped.setdefault(name, []).append(event)
        if len(grouped) != 2:
            raise ValueError("flat events must cover exactly two links")
        return list(grouped.items()), window, groups

    if len(scenario) == 2 and all(
            isinstance(value, (list, tuple)) for value in scenario.values()):
        return list(scenario.items()), window, groups

    raise ValueError(
        "scenario needs links/paths, a/b, or a flat events list")


def _group_bounds(all_events, explicit):
    """Combine explicit group bounds with any inline event bounds.

    A group named by an event but given no bounds is treated as unbounded:
    the site supplied no skew range, so the whole group may sit anywhere.
    """
    bounds = dict(explicit)
    for event in all_events:
        if event.group is not None and event.group not in bounds:
            bounds[event.group] = (None, None)

    collected = {}
    for event in all_events:
        if event.group is None:
            continue
        if event.inline_offset_bounds is None:
            continue
        collected.setdefault(event.group, []).append(
            event.inline_offset_bounds)
    for group, ranges in collected.items():
        ranges = list(ranges)
        if bounds.get(group) is not None:
            ranges.append(bounds[group])
        bounds[group] = _intersect_ranges(ranges, 'clock group %r' % (group,))
    return bounds


def simultaneous_blindness_range(events_a, events_b=None, window=None,
                                 clock_groups=None):
    """Compute the possible duration of simultaneous blindness.

    ``events_a`` and ``events_b`` are sequences of outage records.  A record
    may be a :class:`BlindnessEvent`, a mapping, or a tuple/list of
    ``(earliest_start, latest_end, core_start, core_end[, group])``.  A single
    mapping containing the whole scenario may be passed as ``events_a``.

    ``clock_groups`` maps a group name to a fixed offset or to an
    ``(offset_min, offset_max)`` pair.  Every event in the same group is
    translated by exactly the same chosen offset.  Group bounds may also be
    supplied inline on the events; multiple inline bounds in one group are
    intersected.

    ``window`` is ``(begin, end)`` on the same timeline as the events.  Only
    the part of the joint-blind set inside the window is measured.

    Returns a :class:`BlindnessResult`.  Add the segments in
    ``result.lower.overlap`` (respectively ``result.upper.overlap``) to
    recover the bound; ``result.lower.offsets`` says where each group sat.
    """
    if events_b is None and isinstance(events_a, dict):
        paths, scenario_window, scenario_groups = _scenario_paths(events_a)
        if window is None:
            window = scenario_window
        if clock_groups is None:
            clock_groups = scenario_groups
        links = [name for name, _ in paths]
        path_events = [(name, _coerce_events(value, name))
                       for name, value in paths]
    else:
        if events_b is None:
            raise TypeError("pass two event lists or one scenario mapping")
        links = ['A', 'B']
        path_events = [
            ('A', _coerce_events(events_a, 'A')),
            ('B', _coerce_events(events_b, 'B')),
        ]

    if len(path_events) != 2:
        raise ValueError("simultaneous blindness needs exactly two links")

    all_events = [event for _, events in path_events for event in events]
    if not all_events:
        raise ValueError("at least one outage event is required")

    if window is None:
        window = (
            min(event.earliest_start for event in all_events),
            max(event.latest_end for event in all_events),
        )
    window = _coerce_window(window)

    groups = _group_bounds(all_events, _coerce_groups(clock_groups))
    groups, fixed, variable_groups, lows, highs = _finite_box(
        all_events, window, groups)

    event_a = path_events[0][1]
    event_b = path_events[1][1]

    bounds = {}
    for mode, kind, minimise in (('core', 'lower', True),
                                 ('full', 'upper', False)):
        duration, offsets, overlap, coverage = _best_offsets(
            event_a, event_b, fixed, variable_groups, lows, highs,
            window, mode, minimise)
        intervals = {}
        for name, events in path_events:
            intervals[name] = [
                event.shifted(offsets.get(event.group, 0), mode)
                for event in events
            ]
        coverage_by_link = dict(zip(links, coverage))
        if sorted(coverage_by_link) != sorted(links):
            coverage_by_link = dict(
                (name, _merge_segments(intervals[name], window))
                for name in links)
        bounds[kind] = BlindnessBound(
            kind, duration, dict(offsets), intervals,
            coverage_by_link, overlap, window)

    return BlindnessResult(
        bounds['lower'], bounds['upper'], window, links)


def simultaneous_blindness_bounds(events_a, events_b=None, window=None,
                                  clock_groups=None):
    """Return ``(minimum, maximum)`` as plain numbers."""
    result = simultaneous_blindness_range(
        events_a, events_b, window, clock_groups)
    return result.minimum, result.maximum


def solve_scenario(scenario):
    """Solve a mapping-form scenario."""
    return simultaneous_blindness_range(scenario)


def solve(*args, **kwargs):
    """Alias for :func:`simultaneous_blindness_range`."""
    return simultaneous_blindness_range(*args, **kwargs)
simultaneous_outage_range = simultaneous_blindness_range
possible_blindness_range = simultaneous_blindness_range
blindness_range = simultaneous_blindness_range


# ---------------------------------------------------------------------------
# Human-readable witness
# ---------------------------------------------------------------------------

def format_witness(result, digits=None):
    """Format both bounds as a timeline a duty operator can re-add.

    Each bound starts with its offsets, then lists every shifted interval and
    the merged joint-blind intersection, so the printed total can be checked
    by adding the printed segment lengths.
    """
    def number(value):
        if digits is None:
            return ('%s' % value)
        return ('%.*f' % (digits, value))

    lines = []
    window = result.window
    lines.append("query window: [%s, %s)" %
                 (number(window[0]), number(window[1])))
    for bound in (result.lower, result.upper):
        lines.append("")
        lines.append("%s bound: %s" %
                     (bound.kind, number(bound.duration)))
        if bound.offsets:
            lines.append("  clock offsets:")
            for group in sorted(bound.offsets, key=lambda value: str(value)):
                lines.append("    %s = %s" %
                             (group, number(bound.offsets[group])))
        for name in result.links:
            lines.append("  %s intervals: %s" % (
                name,
                ', '.join('[%s, %s)' % (number(begin), number(end))
                          for begin, end in bound.intervals[name]) or '-'))
            lines.append("  %s coverage:  %s" % (
                name,
                ', '.join('[%s, %s)' % (number(begin), number(end))
                          for begin, end in bound.coverage[name]) or '-'))
        lines.append("  intersection:")
        if bound.overlap:
            for begin, end in bound.overlap:
                lines.append("    [%s, %s)  length %s" % (
                    number(begin), number(end), number(end - begin)))
        else:
            lines.append("    -")
        lines.append("  total: %s" % number(bound.duration))
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Command-line entry point
# ---------------------------------------------------------------------------

def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    text = json_output = False
    path = None
    for argument in argv:
        if argument == '--text':
            text = True
        elif argument == '--json':
            json_output = True
        elif argument in ('-', '--stdin'):
            path = '-'
        elif argument.startswith('--'):
            sys.stderr.write("unknown option: %s\n" % argument)
            return 2
        elif path is None:
            path = argument
        else:
            sys.stderr.write(
                "usage: python -m intervaltree.blindness "
                "[--json|--text] [scenario.json]\n")
            return 2
    if text and json_output:
        sys.stderr.write("choose either --json or --text\n")
        return 2

    if path is not None and path != '-':
        with open(path, 'r') as handle:
            scenario = json.load(handle)
    else:
        scenario = json.load(sys.stdin)
    try:
        result = solve_scenario(scenario)
    except (TypeError, ValueError) as error:
        sys.stderr.write("invalid scenario: %s\n" % (error,))
        return 1

    if text and not json_output:
        print(format_witness(result))
    else:
        json.dump(result.to_dict(), sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
