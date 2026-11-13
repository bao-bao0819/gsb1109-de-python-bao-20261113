#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Integer pulse allocation across nested, half-open time segments.

A master ledger publishes an integer number of pulses for one overall
time window.  Downstream users often need to split that single number
into additive sub-ledgers: first by shift, then by work section inside
each shift.  This module performs that split without losing or inventing
pulses.

Rules implemented here:

* Every time range is half-open, ``[begin, end)``.  A cut point is
  therefore owned by the segment beginning at that point, not by the
  segment ending there.
* Pulses whose exact instants are known are assigned before any
  duration-based apportionment.  Their containing segment is selected by
  the same half-open rule.
* The remaining integer pulses are apportioned in proportion to each
  segment's *effective* duration.  Effective duration is the part of the
  segment inside the master window, with downtime subtracted.
* Apportionment uses the largest-remainder method.  Every pulse awarded
  beyond a segment's floor share is recorded, so the tie-breaking and
  remainder assignment can be audited later.
* Sub-ledger totals always add up to their parent's published total.
  This holds at both levels: shifts sum to the master total, and sections
  sum to their shift total.

The public entry point is :func:`allocate_pulses`.  Its result is an
:class:`Allocation` object (also exported as ``PulseLedger``).
"""

from __future__ import division

from collections import OrderedDict
from decimal import Decimal
from fractions import Fraction
from numbers import Integral, Rational

try:
    from collections.abc import Mapping
except ImportError:  # pragma: no cover - Python 2
    from collections import Mapping

try:
    from datetime import timedelta
except ImportError:  # pragma: no cover
    timedelta = ()  # pragma: no cover


__all__ = [
    'Allocation',
    'AllocationEntry',
    'AllocationError',
    'AmbiguousPulseError',
    'InvalidLedgerError',
    'Ledger',
    'LedgerError',
    'Period',
    'Pulse',
    'PulseAllocation',
    'PulseAllocationError',
    'PulseAssignment',
    'PulseLedger',
    'RemainderAward',
    'SubledgerError',
    'UnallocatedPulseError',
    'UnassignedPulseError',
    'allocate',
    'allocate_pulses',
    'apportion',
    'apportion_pulses',
    'distribute_pulses',
    'pulse_ledger',
    'split_ledger',
    'split_pulses',
]


class LedgerError(ValueError):
    """Base class for pulse-ledger validation errors."""


class InvalidLedgerError(LedgerError):
    """The supplied ledger or its segments are malformed."""


class AllocationError(LedgerError):
    """The requested integer split cannot be performed."""


class UnallocatedPulseError(AllocationError):
    """A known pulse instant is not covered by the requested hierarchy."""


class AmbiguousPulseError(UnallocatedPulseError):
    """A known pulse instant falls into more than one unrelated segment."""


# Friendly aliases used by callers who describe the same failures
# in domain terms.
PulseAllocationError = AllocationError
SubledgerError = AllocationError
UnassignedPulseError = UnallocatedPulseError


_MISSING = object()


def _first_attr(obj, names, default=_MISSING):
    for name in names:
        if isinstance(obj, Mapping) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    if default is _MISSING:
        raise InvalidLedgerError(
            "Missing one of these fields: {0}".format(", ".join(names))
        )
    return default


def _is_mapping(value):
    return isinstance(value, Mapping)


def _is_range_pair(value):
    """Whether ``value`` plausibly describes one ``(begin, end)`` pair."""
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        return False
    first, second = value
    # A collection of two ranges is not itself one range.
    if isinstance(first, (tuple, list)) or isinstance(second, (tuple, list)):
        return False
    if isinstance(first, Mapping) or isinstance(second, Mapping):
        return False
    if hasattr(first, 'begin') or hasattr(second, 'begin'):
        return False
    try:
        # Ordering is validated later; comparability is what matters here.
        first < second
    except TypeError:
        try:
            first > second
        except TypeError:
            return False
    return True


def _as_bound_pair(value, what="range"):
    """
    Coerce a range-like object to ``(begin, end)``.

    Accepted forms are ``(begin, end)``, ``Interval`` objects, mappings
    with begin/end-like keys, and objects exposing those attributes.

    Attribute lookup happens before sequence unpacking because
    ``Interval`` is itself a namedtuple; treating it as a plain sequence
    would misread its three fields.
    """
    if isinstance(value, Mapping):
        begin = _first_attr(
            value,
            ('begin', 'start', 'from', 'lower', 'open', 'left'),
            default=_MISSING,
        )
        end = _first_attr(
            value,
            ('end', 'stop', 'to', 'upper', 'close', 'right'),
            default=_MISSING,
        )
        if begin is _MISSING or end is _MISSING:
            raise InvalidLedgerError(
                "{0} must provide begin and end values".format(what)
            )
        return begin, end

    if hasattr(value, 'begin') and hasattr(value, 'end'):
        return value.begin, value.end

    if isinstance(value, (tuple, list)):
        if len(value) != 2:
            raise InvalidLedgerError(
                "{0} must be a (begin, end) pair".format(what)
            )
        return value[0], value[1]

    begin = _first_attr(
        value,
        ('begin', 'start', 'from', 'lower', 'open', 'left'),
        default=_MISSING,
    )
    end = _first_attr(
        value,
        ('end', 'stop', 'to', 'upper', 'close', 'right'),
        default=_MISSING,
    )
    if begin is _MISSING or end is _MISSING:
        raise InvalidLedgerError(
            "{0} must provide begin and end values".format(what)
        )
    return begin, end


def _validate_range(begin, end, what):
    try:
        valid = begin < end
    except TypeError:
        raise InvalidLedgerError(
            "{0} bounds are not comparable: {1!r}, {2!r}".format(
                what, begin, end
            )
        )
    if not valid:
        raise InvalidLedgerError(
            "{0} must have begin < end, got [{1!r}, {2!r})".format(
                what, begin, end
            )
        )


def _looks_like_period_mapping(value):
    if not _is_mapping(value):
        return False
    keys = set(value)
    begin_keys = set(('begin', 'start', 'from', 'lower', 'open', 'left'))
    end_keys = set(('end', 'stop', 'to', 'upper', 'close', 'right'))
    return bool(keys & begin_keys) and bool(keys & end_keys)


class Period(object):
    """
    A named, half-open time segment.

    ``parent`` is used for sections to identify the owning shift.  It is
    ``None`` for shifts.
    """

    __slots__ = ('key', 'begin', 'end', 'parent', 'order')

    def __init__(self, key, begin, end, parent=None, order=0):
        _validate_range(begin, end, "period {0!r}".format(key))
        self.key = key
        self.begin = begin
        self.end = end
        self.parent = parent
        self.order = order

    def contains(self, point):
        """Whether ``point`` belongs to this half-open period."""
        return self.begin <= point < self.end

    def contains_period(self, other):
        """Whether ``other`` is wholly inside this period."""
        return self.begin <= other.begin and self.end >= other.end

    @property
    def length(self):
        return self.end - self.begin

    def as_dict(self):
        return {
            'key': self.key,
            'begin': self.begin,
            'end': self.end,
            'parent': self.parent,
        }

    def __repr__(self):
        return "Period({0!r}, {1!r}, {2!r}{3})".format(
            self.key,
            self.begin,
            self.end,
            "" if self.parent is None else ", parent={0!r}".format(self.parent),
        )


class Pulse(object):
    """A known pulse instant and its integer multiplicity."""

    __slots__ = ('timestamp', 'count', 'key')

    def __init__(self, timestamp, count=1, key=None):
        if not _is_integer(count):
            raise InvalidLedgerError(
                "pulse count must be an integer, got {0!r}".format(count)
            )
        if count < 0:
            raise InvalidLedgerError(
                "pulse count must be non-negative, got {0!r}".format(count)
            )
        self.timestamp = timestamp
        self.count = int(count)
        self.key = key

    def as_dict(self):
        return {'timestamp': self.timestamp, 'count': self.count, 'key': self.key}

    def __repr__(self):
        return "Pulse({0!r}, count={1!r})".format(self.timestamp, self.count)


def _is_integer(value):
    return isinstance(value, Integral) and not isinstance(value, bool)


def _split_three_tuple(value, what):
    """
    Interpret a three-item tuple as either ``(key, begin, end)`` or
    ``(begin, end, key)``.

    The latter mirrors ``Interval(begin, end, data)``.  When all three
    values are numeric, prefer whichever interpretation produces a
    valid, non-empty range.
    """
    first, second, third = value
    numeric = (int, float, Decimal, Fraction)
    first_numeric = isinstance(first, numeric)
    second_numeric = isinstance(second, numeric)
    third_numeric = isinstance(third, numeric)

    if first_numeric and second_numeric and not third_numeric:
        # ``Interval(begin, end, data)`` layout.
        return third, first, second
    if second_numeric and third_numeric and not first_numeric:
        return first, second, third
    if first_numeric and second_numeric and third_numeric:
        key_first_valid = second < third
        key_last_valid = first < second
        if key_first_valid and not key_last_valid:
            return first, second, third
        if key_last_valid and not key_first_valid:
            return third, first, second
        # Both interpretations describe a non-empty range.  The common
        # ``(key, begin, end)`` layout wins.
        return first, second, third
    # A string-like first item is the key in the common case.
    return first, second, third


def _coerce_period(item, order, default_key=None, parent=None):
    """Turn an interval-like item into a :class:`Period`."""
    if isinstance(item, Period):
        key = item.key
        begin, end = item.begin, item.end
        if item.parent is not None:
            parent = item.parent
        return Period(key, begin, end, parent=parent, order=order)

    # ``Interval`` is a namedtuple, so check its data field before
    # treating the object as a plain sequence.
    if hasattr(item, 'begin') and hasattr(item, 'end'):
        begin, end = item.begin, item.end
        key = _first_attr(
            item,
            ('key', 'name', 'id', 'label', 'section', 'shift', 'data'),
            default=default_key,
        )
        shift_key = _first_attr(item, ('shift', 'parent_shift', 'shift_key'),
                                default=None)
        if shift_key is not None:
            parent = shift_key
        if key is None:
            key = order
        return Period(key, begin, end, parent=parent, order=order)

    if isinstance(item, Mapping):
        key = _first_attr(
            item, ('key', 'name', 'id', 'label', 'section'),
            default=None,
        )
        shift_key = _first_attr(item, ('shift', 'parent_shift', 'shift_key'),
                                default=None)
        if shift_key is not None:
            parent = shift_key
        begin, end = _as_bound_pair(item, "period")
        if key is None:
            key = default_key
        if key is None:
            key = order
        return Period(key, begin, end, parent=parent, order=order)

    if isinstance(item, (tuple, list)):
        if len(item) == 2:
            begin, end = item
            key = default_key if default_key is not None else order
        elif len(item) == 3:
            key, begin, end = _split_three_tuple(item, "period")
            if key is None:
                key = default_key if default_key is not None else order
        elif len(item) == 4:
            # (shift, section, begin, end)
            parent, key, begin, end = item
        else:
            raise InvalidLedgerError(
                "period tuples must have 2, 3 or 4 items, got {0!r}".format(item)
            )
        return Period(key, begin, end, parent=parent, order=order)

    # Objects exposing begin/end, including Interval.
    begin, end = _as_bound_pair(item, "period")
    key = _first_attr(
        item,
        ('key', 'name', 'id', 'label', 'section', 'shift', 'data'),
        default=default_key,
    )
    shift_key = _first_attr(item, ('shift', 'parent_shift', 'shift_key'),
                            default=None)
    if shift_key is not None:
        parent = shift_key
    if key is None:
        key = order
    return Period(key, begin, end, parent=parent, order=order)


def _normalize_periods(items, what):
    """Normalize shifts or flat section descriptions."""
    if items is None:
        return []

    if isinstance(items, Period):
        return [items]

    # ``IntervalTree`` and other iterables of intervals are accepted.
    unordered = isinstance(items, (set, frozenset))
    if not isinstance(items, (Mapping, list, tuple, set, frozenset)):
        try:
            iterator = iter(items)
        except TypeError:
            items = [items]
        else:
            items = list(iterator)
            unordered = True

    periods = []
    index = 0

    if _is_mapping(items):
        if _looks_like_period_mapping(items):
            # A single period mapping.
            periods.append(_coerce_period(items, 0))
        else:
            for key, value in items.items():
                if isinstance(value, Mapping) and not _looks_like_period_mapping(value):
                    # Nested collections are handled by the sections code.
                    raise InvalidLedgerError(
                        "unexpected nested {0} value for {1!r}".format(what, key)
                    )
                if isinstance(value, (tuple, list)):
                    if len(value) == 2 and _is_range_pair(value):
                        begin, end = value
                        periods.append(Period(key, begin, end, order=index))
                    elif len(value) in (3, 4):
                        period = _coerce_period(value, index, default_key=key)
                        periods.append(period)
                    else:
                        raise InvalidLedgerError(
                            "invalid {0} value for {1!r}: {2!r}".format(
                                what, key, value
                            )
                        )
                else:
                    begin, end = _as_bound_pair(value, what)
                    periods.append(Period(key, begin, end, order=index))
                index += 1
        _check_duplicate_keys(periods, what)
        return periods

    for item in items:
        period = _coerce_period(item, index)
        periods.append(period)
        index += 1

    if unordered:
        # Sets and arbitrary iterables have no meaningful input order.
        # Sorting by start makes remainder ties reproducible.
        periods.sort(key=lambda period: (period.begin, period.end))
        for order, period in enumerate(periods):
            period.order = order

    _check_duplicate_keys(periods, what)
    return periods


def _check_duplicate_keys(periods, what):
    seen = set()
    for period in periods:
        if period.key in seen:
            raise InvalidLedgerError(
                "duplicate {0} key: {1!r}".format(what, period.key)
            )
        seen.add(period.key)


def _iter_section_value(value):
    """Yield section specification items from one shift's value."""
    if _is_mapping(value):
        if _looks_like_period_mapping(value):
            yield value
        else:
            for key, spec in value.items():
                if isinstance(spec, Mapping) and _looks_like_period_mapping(spec):
                    item = dict(spec)
                    if _first_attr(item, ('key', 'name', 'id', 'label'),
                                   default=None) is None:
                        item['key'] = key
                    yield item
                elif isinstance(spec, (tuple, list)):
                    if len(spec) == 2:
                        yield (key, spec[0], spec[1])
                    elif len(spec) == 3:
                        yield (key,) + tuple(spec)
                    elif len(spec) == 4:
                        yield tuple(spec)
                    else:
                        raise InvalidLedgerError(
                            "invalid section value for {0!r}: {1!r}".format(key, spec)
                        )
                elif isinstance(spec, Period):
                    yield spec
                else:
                    begin, end = _as_bound_pair(spec, "section")
                    yield (key, begin, end)
    elif isinstance(value, (tuple, list)) and len(value) == 2 and _is_range_pair(value):
        yield value
    else:
        for item in value:
            yield item


def _normalize_sections(sections, shifts):
    """
    Return ``OrderedDict(shift_key -> [Period, ...])``.

    Supported common forms::

        {shift: {section: (begin, end)}}
        {shift: [(section, begin, end), ...]}
        {section: (begin, end)}                 # only one shift
        [(shift, section, begin, end), ...]
        [{'shift': shift, 'section': section, 'begin': b, 'end': e}, ...]
    """
    result = OrderedDict((shift.key, []) for shift in shifts)
    if sections is None:
        return result

    shift_keys = set(result)
    flat = []

    if _is_mapping(sections):
        for key, value in sections.items():
            nested = False
            if key in shift_keys:
                if _is_mapping(value) and not _looks_like_period_mapping(value):
                    nested = True
                elif (isinstance(value, (tuple, list)) and
                      not _is_range_pair(value)):
                    nested = True

            if nested:
                index = 0
                for item in _iter_section_value(value):
                    period = _coerce_period(item, index)
                    if period.parent is not None and period.parent != key:
                        raise InvalidLedgerError(
                            "section {0!r} declares shift {1!r} but is nested "
                            "under {2!r}".format(period.key, period.parent, key)
                        )
                    period.parent = key
                    result[key].append(period)
                    index += 1
                continue

            period = _coerce_period(value, 0, default_key=key)
            flat.append(period)
    else:
        for index, item in enumerate(sections):
            if isinstance(item, (tuple, list)) and len(item) == 4:
                shift_key, section_key, begin, end = item
                flat.append(Period(section_key, begin, end,
                                   parent=shift_key, order=index))
            else:
                flat.append(_coerce_period(item, index))

    for period in flat:
        shift_key = period.parent
        if shift_key is None:
            if len(shifts) == 1:
                shift_key = shifts[0].key
            else:
                # A flat section list is common.  If its bounds fall
                # wholly inside exactly one shift, infer that owner.
                owners = [shift for shift in shifts
                          if shift.contains_period(period)]
                if len(owners) == 1:
                    shift_key = owners[0].key
                else:
                    raise InvalidLedgerError(
                        "section {0!r} has no unambiguous owning shift; add "
                        "a 'shift' field, nest it under a shift, or provide "
                        "only one shift".format(period.key)
                    )
        if shift_key not in result:
            raise InvalidLedgerError(
                "section {0!r} refers to unknown shift {1!r}".format(
                    period.key, shift_key
                )
            )
        period.parent = shift_key
        result[shift_key].append(period)

    for shift_key, periods in result.items():
        _check_duplicate_keys(periods, "section")
    return result


def _coerce_pulse(item):
    if isinstance(item, Pulse):
        return item
    if isinstance(item, Mapping):
        timestamp = _first_attr(
            item, ('timestamp', 'time', 'at', 'instant', 'point', 'begin'),
            default=None,
        )
        if timestamp is None:
            raise InvalidLedgerError("pulse mapping has no timestamp")
        count = _first_attr(item, ('count', 'pulses', 'n', 'value'), default=1)
        key = _first_attr(item, ('key', 'id', 'label', 'name'), default=None)
        return Pulse(timestamp, count, key)
    if isinstance(item, (tuple, list)) and len(item) == 2:
        return Pulse(item[0], item[1])
    return Pulse(item, 1)


def _normalize_pulses(pulses):
    if pulses is None:
        return []
    if isinstance(pulses, Pulse):
        return [pulses]
    if _is_mapping(pulses):
        result = []
        for timestamp in sorted(pulses):
            result.append(Pulse(timestamp, pulses[timestamp]))
        return result
    # A single scalar instant is the overwhelmingly common case.
    if not isinstance(pulses, (tuple, list, set, frozenset)):
        try:
            iter(pulses)
        except TypeError:
            return [Pulse(pulses, 1)]
    if isinstance(pulses, tuple) and len(pulses) == 2:
        # A two-item tuple is a single ``(timestamp, count)`` pulse.
        # Use a list when two separate instants are intended.
        return [Pulse(pulses[0], pulses[1])]
    return [_coerce_pulse(item) for item in pulses]


def _resolve_window(window, begin, end, shifts, total):
    if window is not None:
        if begin is not None or end is not None:
            raise InvalidLedgerError(
                "provide either window or begin/end, not both"
            )
        begin, end = _as_bound_pair(window, "master window")
    elif begin is None and end is None:
        if shifts:
            begin = min(shift.begin for shift in shifts)
            end = max(shift.end for shift in shifts)
        elif total == 0:
            return None, None
        else:
            raise InvalidLedgerError(
                "a master window is required when there are no shifts"
            )
    elif begin is None or end is None:
        raise InvalidLedgerError("both begin and end are required")
    _validate_range(begin, end, "master window")
    return begin, end


def _normalize_downtime(downtime):
    if downtime is None:
        return []
    # A single (begin, end) pair may itself arrive as a list or tuple.
    if _is_range_pair(downtime):
        downtime = [downtime]
    if _is_mapping(downtime):
        try:
            begin, end = _as_bound_pair(downtime, "downtime")
        except InvalidLedgerError:
            items = [downtime[key] for key in sorted(downtime)]
        else:
            items = [(begin, end)]
    elif hasattr(downtime, 'begin') and hasattr(downtime, 'end'):
        # ``Interval`` and similar objects are single ranges even
        # though they are also sequences.
        items = [downtime]
    elif isinstance(downtime, (tuple, list)):
        items = list(downtime)
    else:
        # A single interval-like object describing one downtime range.
        items = [downtime]

    ranges = []
    for item in items:
        begin, end = _as_bound_pair(item, "downtime")
        try:
            if begin >= end:
                continue
        except TypeError:
            raise InvalidLedgerError(
                "downtime bounds are not comparable: {0!r}, {1!r}".format(
                    begin, end
                )
            )
        ranges.append((begin, end))

    ranges.sort(key=lambda pair: pair[0])
    merged = []
    for begin, end in ranges:
        if not merged:
            merged.append([begin, end])
            continue
        last = merged[-1]
        if begin <= last[1]:
            if end > last[1]:
                last[1] = end
        else:
            merged.append([begin, end])
    return [(pair[0], pair[1]) for pair in merged]


def _subtract_downtime(lo, hi, downtime):
    """Length of ``[lo, hi)`` after removing all downtime."""
    length = hi - lo
    for down_begin, down_end in downtime:
        overlap_begin = max(lo, down_begin)
        overlap_end = min(hi, down_end)
        if overlap_end > overlap_begin:
            length = length - (overlap_end - overlap_begin)
    return length


def _effective_duration(period, window_begin, window_end, downtime,
                        clip_begin=None, clip_end=None):
    lo = period.begin
    hi = period.end
    if clip_begin is not None and clip_begin > lo:
        lo = clip_begin
    if clip_end is not None and clip_end < hi:
        hi = clip_end
    if window_begin is not None and window_begin > lo:
        lo = window_begin
    if window_end is not None and window_end < hi:
        hi = window_end
    if hi <= lo:
        try:
            return hi - hi
        except TypeError:  # pragma: no cover
            return 0
    return _subtract_downtime(lo, hi, downtime)


def _duration_fraction(value):
    """Convert a duration to an exact Fraction for apportionment."""
    if isinstance(value, timedelta):
        seconds = value.days * 86400 + value.seconds
        return Fraction(seconds * 1000000 + value.microseconds, 1000000)
    if isinstance(value, Decimal):
        return Fraction(value)
    if isinstance(value, Rational):
        return Fraction(value)
    if isinstance(value, float):
        return Fraction(value)
    if isinstance(value, Integral):
        return Fraction(int(value), 1)
    return Fraction(value)


class AllocationEntry(object):
    """
    One shift or section row in an allocation.

    ``exact`` is the exact duration-proportional share of the pulses left
    after known instants, before integer rounding.  ``fraction`` is its
    fractional part.  ``extra`` is the number of remainder pulses awarded
    to this row, which is the quantity to inspect when auditing where a
    rounding remainder went.
    """

    __slots__ = (
        'level', 'parent', 'key', 'begin', 'end', 'duration', 'known',
        'remaining', 'exact', 'floor', 'fraction', 'extra', 'total',
        'rank', 'order', 'implicit',
    )

    def __init__(self, level, parent, key, begin, end, duration, known,
                 remaining, exact, floor, fraction, extra, total, rank,
                 order, implicit=False):
        self.level = level
        self.parent = parent
        self.key = key
        self.begin = begin
        self.end = end
        self.duration = duration
        self.known = known
        self.remaining = remaining
        self.exact = exact
        self.floor = floor
        self.fraction = fraction
        self.extra = extra
        self.total = total
        self.rank = rank
        self.order = order
        self.implicit = implicit

    @property
    def quota(self):
        """Exact duration-proportional share as a float."""
        return float(self.exact)

    @property
    def apportioned(self):
        """Pulses assigned by duration, including any remainder."""
        return self.floor + self.extra

    @property
    def remainder(self):
        """Number of remainder pulses awarded to this row."""
        return self.extra

    @property
    def got_remainder(self):
        return self.extra != 0

    @property
    def share(self):
        """Final integer total for this row."""
        return self.total

    def as_dict(self):
        return {
            'level': self.level,
            'parent': self.parent,
            'key': self.key,
            'begin': self.begin,
            'end': self.end,
            'duration': self.duration,
            'known': self.known,
            'remaining': self.remaining,
            'exact': float(self.exact),
            'quota': self.quota,
            'floor': self.floor,
            'fraction': float(self.fraction),
            'remainder': self.extra,
            'extra': self.extra,
            'total': self.total,
            'rank': self.rank,
            'got_remainder': self.got_remainder,
            'implicit': self.implicit,
        }

    def __repr__(self):
        return (
            "AllocationEntry(level={0!r}, key={1!r}, known={2!r}, "
            "quota={3!r}, remainder={4!r}, total={5!r})"
        ).format(
            self.level, self.key, self.known, self.quota, self.extra, self.total
        )


class PulseAssignment(object):
    """Audit record for one known pulse instant."""

    __slots__ = ('timestamp', 'count', 'shift', 'section', 'key', 'order',
                 'rule')

    def __init__(self, timestamp, count, shift, section, key=None, order=0,
                 rule='half-open containment'):
        self.timestamp = timestamp
        self.count = count
        self.shift = shift
        self.section = section
        self.key = key
        self.order = order
        self.rule = rule

    def as_dict(self):
        return {
            'timestamp': self.timestamp,
            'count': self.count,
            'shift': self.shift,
            'section': self.section,
            'key': self.key,
            'rule': self.rule,
        }

    def __repr__(self):
        return (
            "PulseAssignment({0!r}, count={1!r}, shift={2!r}, section={3!r})"
        ).format(self.timestamp, self.count, self.shift, self.section)


RemainderAward = AllocationEntry


class _Totals(OrderedDict):
    """An ordered mapping that can also be called like a method."""

    def __call__(self):
        return self


class _SectionTotals(OrderedDict):
    """Nested shift -> section totals, callable like a method."""

    def __call__(self):
        return self


class Allocation(object):
    """
    Result of :func:`allocate_pulses`.

    The additive views are ``shift_totals`` and ``section_totals``.
    The aliases ``by_shift`` and ``by_section`` are callable as well as
    subscriptable, so both ``result.by_shift['A']`` and
    ``result.by_shift()['A']`` work.
    """

    def __init__(self, total, window, shift_totals, section_totals,
                 entries, assignments, remainders=None):
        self.total = total
        self.window = window
        self.shift_totals = shift_totals
        self.section_totals = section_totals
        self.entries = list(entries)
        self.assignments = list(assignments)
        self.remainders = list(
            remainders if remainders is not None
            else [entry for entry in self.entries if entry.got_remainder]
        )

    @property
    def master_total(self):
        return self.total

    @property
    def total_pulses(self):
        return self.total

    @property
    def by_shift(self):
        return self.shift_totals

    @property
    def by_section(self):
        return self.section_totals

    @property
    def known_assignments(self):
        return self.assignments

    @property
    def known_pulses(self):
        return self.assignments

    @property
    def remainder_audit(self):
        return self.remainders

    @property
    def shift_entries(self):
        return [entry for entry in self.entries if entry.level == 'shift']

    @property
    def section_entries(self):
        return [entry for entry in self.entries if entry.level == 'section']

    @property
    def sections(self):
        """All section entries in shift order, with a flat ``key`` view."""
        return self.section_entries

    def section_total(self, shift_key, section_key=None):
        """Look up one section's total, or a shift's section mapping."""
        if section_key is None:
            return self.section_totals[shift_key]
        return self.section_totals[shift_key][section_key]

    @property
    def award_rule(self):
        """
        Description of how remainder pulses were awarded.

        Rows are ranked by larger fractional share, then earlier begin,
        then earlier end, then input order.  Each remainder pulse goes to
        the highest-ranked row that has not already received one in that
        round.
        """
        return 'largest-remainder; ties by earliest begin, then end, ' \
               'then input order'

    @property
    def master_window(self):
        return self.window

    def __getitem__(self, key):
        return self.shift_totals[key]

    def __iter__(self):
        return iter(self.shift_totals)

    def __len__(self):
        return len(self.shift_totals)

    def keys(self):
        return self.shift_totals.keys()

    def values(self):
        return self.shift_totals.values()

    def items(self):
        return self.shift_totals.items()

    def get(self, key, default=None):
        return self.shift_totals.get(key, default)

    def summary(self):
        return {
            'total': self.total,
            'window': self.window,
            'by_shift': dict(self.shift_totals),
            'by_section': dict(
                (shift, dict(sections))
                for shift, sections in self.section_totals.items()
            ),
            'award_rule': self.award_rule,
            'remainders': [entry.as_dict() for entry in self.remainders],
            'known_pulses': [item.as_dict() for item in self.assignments],
        }

    as_dict = summary

    def check(self):
        """Verify all additive invariants, raising on failure."""
        shift_sum = sum(self.shift_totals.values())
        if shift_sum != self.total:
            raise AllocationError(
                "shift sub-ledgers sum to {0!r}, expected {1!r}".format(
                    shift_sum, self.total
                )
            )
        for shift_key, shift_total in self.shift_totals.items():
            sections = self.section_totals.get(shift_key, {})
            section_sum = sum(sections.values())
            if section_sum != shift_total:
                raise AllocationError(
                    "sections of shift {0!r} sum to {1!r}, expected {2!r}".format(
                        shift_key, section_sum, shift_total
                    )
                )
        return True

    assert_balanced = check

    def __repr__(self):
        return "Allocation(total={0!r}, shifts={1!r})".format(
            self.total, dict(self.shift_totals)
        )


def _apportion(total, nodes, level, parent_key, entries):
    """
    Apportion ``total`` among ``nodes``.

    ``nodes`` have ``known``, ``duration``, ``begin``, ``end`` and
    ``order`` attributes.  Returns an ordered mapping of node key to the
    integer total.
    """
    if total < 0:
        raise AllocationError("cannot apportion a negative total")

    known_sum = sum(node.known for node in nodes)
    if known_sum > total:
        raise AllocationError(
            "known pulses ({0}) exceed the published total ({1})".format(
                known_sum, total
            )
        )

    remaining = total - known_sum
    weights = []
    for node in nodes:
        weight = _duration_fraction(node.duration)
        if weight < 0:
            weight = Fraction(0, 1)
        weights.append(weight)
    weight_sum = sum(weights, Fraction(0, 1))

    quotas = [Fraction(0, 1) for node in nodes]
    floors = [0 for node in nodes]
    fractions = [Fraction(0, 1) for node in nodes]
    extras = [0 for node in nodes]

    if remaining and weight_sum == 0:
        if len(nodes) == 1:
            # Degenerate but unambiguous: there is nowhere else to put
            # the published pulses.
            quotas[0] = Fraction(remaining, 1)
            floors[0] = remaining
            extras[0] = 0
        else:
            raise AllocationError(
                "cannot apportion {0} pulse(s) across {1} segments with no "
                "effective duration; exclude downtime or supply known "
                "pulse instants".format(remaining, len(nodes))
            )
    elif remaining:
        for index, weight in enumerate(weights):
            quota = Fraction(remaining, 1) * weight / weight_sum
            quotas[index] = quota
            floors[index] = int(quota)  # quota is non-negative
            fractions[index] = quota - floors[index]

    leftover = remaining - sum(floors)
    # With exact arithmetic, leftover is in [0, number of positive
    # weights).  Use a defensive loop in case a caller supplies unusual
    # numeric durations.
    ranked = _remainder_ranking(nodes, fractions)
    rank_of = {}
    for position, index in enumerate(ranked, 1):
        rank_of[index] = position
    cursor = 0
    while leftover > 0 and ranked:
        index = ranked[cursor % len(ranked)]
        extras[index] += 1
        leftover -= 1
        cursor += 1

    result = OrderedDict()
    for index, node in enumerate(nodes):
        final = node.known + floors[index] + extras[index]
        entry = AllocationEntry(
            level=level,
            parent=parent_key,
            key=node.key,
            begin=node.begin,
            end=node.end,
            duration=node.duration,
            known=node.known,
            remaining=remaining,
            exact=quotas[index],
            floor=floors[index],
            fraction=fractions[index],
            extra=extras[index],
            total=final,
            rank=rank_of.get(index),
            order=node.order,
            implicit=getattr(node, 'implicit', False),
        )
        entries.append(entry)
        result[node.key] = final
    return result


def _remainder_ranking(nodes, fractions):
    """
    Rank nodes for largest-remainder awards.

    Larger fractional shares come first.  Ties go to the earlier
    segment (earlier begin, then earlier end), then to input order.  The
    ranking is returned as indexes into ``nodes``.
    """
    indexes = list(range(len(nodes)))

    def key(index):
        return (-fractions[index], nodes[index].begin,
                nodes[index].end, nodes[index].order)

    try:
        return sorted(indexes, key=key)
    except TypeError:
        # Mixed bound types cannot be compared; input order remains a
        # deterministic tie-breaker.
        return sorted(indexes, key=lambda index: (-fractions[index],
                                                  nodes[index].order))


class _Node(object):
    __slots__ = ('key', 'begin', 'end', 'duration', 'known', 'order',
                 'implicit')

    def __init__(self, key, begin, end, duration, known, order,
                 implicit=False):
        self.key = key
        self.begin = begin
        self.end = end
        self.duration = duration
        self.known = known
        self.order = order
        self.implicit = implicit


def _select_containing(candidates, timestamp, what):
    """Select the unique containing segment, preferring an innermost one."""
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    innermost = []
    for candidate in candidates:
        contains_all = all(
            candidate.begin <= other.begin and candidate.end >= other.end
            for other in candidates
        )
        if contains_all:
            innermost.append(candidate)

    if len(innermost) == 1:
        return innermost[0]

    # Multiple candidates with identical or partially overlapping ranges
    # cannot be resolved accurately.
    raise AmbiguousPulseError(
        "pulse at {0!r} belongs to multiple {1}: {2!r}".format(
            timestamp,
            what,
            [candidate.key for candidate in candidates],
        )
    )


def allocate_pulses(total, shifts=None, sections=None, downtime=None,
                    pulses=None, window=None, begin=None, end=None, **kwargs):
    """
    Split an integer master pulse total into additive shift/section
    sub-ledgers.

    :param total: published integer pulse count for the master window.
    :param shifts: interval-like shift descriptions.  Accepted forms
        include ``{key: (begin, end)}``, ``[(key, begin, end), ...]``,
        ``Interval`` objects, and objects exposing ``begin``/``end``.
    :param sections: optional work-section descriptions nested by shift,
        for example ``{shift: {section: (begin, end)}}`` or a flat list
        of mappings containing a ``shift`` field.
    :param downtime: interval-like ranges that receive no duration
        weight.  Known pulse instants inside downtime are still assigned
        exactly to their containing segment.
    :param pulses: known pulse instants.  A scalar means one pulse; a
        ``(timestamp, count)`` pair carries multiplicity; a mapping of
        ``timestamp -> count`` is also accepted.
    :param window: master ``(begin, end)`` window.  If omitted, the
        envelope of the shifts is used.
    :param begin: master window begin, alternative to ``window``.
    :param end: master window end, alternative to ``window``.
    :return: :class:`Allocation`
    :raises LedgerError: on malformed input, unassignable known pulses,
        or an impossible duration apportionment.

    The algorithm assigns known instants first.  It then apportions the
    still-unassigned pulses among shifts by effective duration, using
    largest remainders, and repeats the same split inside every shift
    for that shift's sections.  Thus ``sum(by_shift.values())`` equals
    ``total`` and each shift's section values sum to its shift value.
    """
    # Common keyword aliases make the function convenient to call from
    # data-pipeline code with different domain vocabularies.
    total = _pop_alias(total, kwargs, ('total_pulses', 'master_total',
                                       'pulse_count', 'count'))
    shapes = _pop_alias(shifts, kwargs, ('shift_intervals', 'classes',
                                         'roster', 'windows'))
    shifts = shapes
    sections = _pop_alias(sections, kwargs, ('work_sections', 'stages',
                                             'areas', 'section_intervals'))
    downtime = _pop_alias(downtime, kwargs, ('downtimes', 'outages',
                                             'closures', 'maintenance',
                                             'stops', 'blackouts'))
    pulses = _pop_alias(pulses, kwargs, ('known_pulses', 'known', 'events',
                                         'instants', 'timestamps',
                                         'known_instants'))
    window = _pop_alias(window, kwargs, ('master', 'master_window', 'span',
                                         'range', 'whole'))
    begin = _pop_alias(begin, kwargs, ('master_begin', 'window_begin',
                                       'start'))
    end = _pop_alias(end, kwargs, ('master_end', 'window_end', 'stop'))
    if kwargs:
        raise TypeError(
            "allocate_pulses() got unexpected keyword argument(s): {0}".format(
                ", ".join(sorted(kwargs))
            )
        )

    if not _is_integer(total):
        raise InvalidLedgerError(
            "master pulse total must be an integer, got {0!r}".format(total)
        )
    total = int(total)
    if total < 0:
        raise InvalidLedgerError("master pulse total must be non-negative")

    shifts = _normalize_periods(shifts, "shift")
    sections_by_shift = _normalize_sections(sections, shifts)
    window_begin, window_end = _resolve_window(
        window, begin, end, shifts, total
    )
    downtime = _normalize_downtime(downtime)
    pulse_list = _normalize_pulses(pulses)

    if not shifts:
        if total:
            raise AllocationError(
                "cannot allocate {0} pulse(s) without any shifts".format(total)
            )
        return Allocation(
            total=total,
            window=(window_begin, window_end),
            shift_totals=_Totals(),
            section_totals=_SectionTotals(),
            entries=[],
            assignments=[],
        )

    # Build shift nodes.
    shift_nodes = []
    shift_durations = {}
    for order, shift in enumerate(shifts):
        duration = _effective_duration(
            shift, window_begin, window_end, downtime
        )
        shift_durations[shift.key] = duration
        shift_nodes.append(_Node(shift.key, shift.begin, shift.end,
                                 duration, 0, order))

    # Build section nodes.  A shift with no explicit sections is treated
    # as one implicit section so that the by-section view remains
    # additive.
    section_nodes = OrderedDict()
    section_periods = OrderedDict()
    for shift in shifts:
        explicit = sections_by_shift.get(shift.key, [])
        if explicit:
            nodes = []
            periods = OrderedDict()
            for order, section in enumerate(explicit):
                duration = _effective_duration(
                    section, window_begin, window_end, downtime,
                    clip_begin=shift.begin, clip_end=shift.end,
                )
                periods[section.key] = section
                nodes.append(_Node(section.key, section.begin, section.end,
                                   duration, 0, order))
            section_nodes[shift.key] = nodes
            section_periods[shift.key] = periods
        else:
            section_nodes[shift.key] = [
                _Node(shift.key, shift.begin, shift.end,
                      shift_durations[shift.key], 0, 0, implicit=True)
            ]
            section_periods[shift.key] = OrderedDict(((shift.key, shift),))

    # Assign known pulse instants exactly, using half-open containment.
    assignments = []
    for order, pulse in enumerate(pulse_list):
        if pulse.count == 0:
            continue
        if window_begin is not None:
            if not (window_begin <= pulse.timestamp < window_end):
                raise UnallocatedPulseError(
                    "known pulse at {0!r} lies outside the master window "
                    "[{1!r}, {2!r})".format(
                        pulse.timestamp, window_begin, window_end
                    )
                )

        shift_candidates = [
            shift for shift in shifts if shift.contains(pulse.timestamp)
        ]
        shift = _select_containing(shift_candidates, pulse.timestamp, "shifts")
        if shift is None:
            raise UnallocatedPulseError(
                "known pulse at {0!r} is not covered by any shift".format(
                    pulse.timestamp
                )
            )

        section_candidates = [
            period for period in section_periods[shift.key].values()
            if period.contains(pulse.timestamp)
        ]
        section = _select_containing(
            section_candidates, pulse.timestamp,
            "sections of shift {0!r}".format(shift.key),
        )
        if section is None:
            raise UnallocatedPulseError(
                "known pulse at {0!r} is inside shift {1!r} but outside all "
                "of its sections".format(pulse.timestamp, shift.key)
            )

        assignments.append(PulseAssignment(
            pulse.timestamp, pulse.count, shift.key, section.key,
            pulse.key, order,
        ))

        shift_node = next(node for node in shift_nodes
                          if node.key == shift.key)
        shift_node.known += pulse.count
        section_node = next(
            node for node in section_nodes[shift.key]
            if node.key == section.key
        )
        section_node.known += pulse.count

    # Allocate shifts, then sections within each shift.
    entries = []
    shift_totals = _apportion(total, shift_nodes, 'shift', None, entries)
    section_totals = _SectionTotals()
    for shift_key in shift_totals:
        nodes = section_nodes[shift_key]
        sections_result = _apportion(
            shift_totals[shift_key], nodes, 'section', shift_key, entries
        )
        section_totals[shift_key] = _Totals(sections_result)

    allocation = Allocation(
        total=total,
        window=(window_begin, window_end),
        shift_totals=_Totals(shift_totals),
        section_totals=section_totals,
        entries=entries,
        assignments=assignments,
    )
    allocation.check()
    return allocation


def _pop_alias(value, kwargs, names):
    """Apply keyword aliases, tolerating an explicitly supplied value."""
    for name in names:
        if name in kwargs:
            candidate = kwargs.pop(name)
            if value is None:
                value = candidate
            elif candidate is not value and candidate != value:
                raise TypeError(
                    "multiple values supplied for {0}".format(name)
                )
    return value


def apportion(total, shifts=None, sections=None, downtime=None, pulses=None,
              window=None, begin=None, end=None, **kwargs):
    """Alias for :func:`allocate_pulses`."""
    return allocate_pulses(
        total, shifts=shifts, sections=sections, downtime=downtime,
        pulses=pulses, window=window, begin=begin, end=end, **kwargs
    )


allocate = apportion
apportion_pulses = apportion
distribute_pulses = apportion
split_pulses = apportion
split_ledger = apportion
pulse_ledger = apportion


class PulseLedger(Allocation):
    """
    Allocation result under a domain-friendly name.

    Constructing it performs the same operation as
    :func:`allocate_pulses`::

        ledger = PulseLedger(10, {'A': (0, 5), 'B': (5, 10)})
    """

    def __init__(self, total, shifts=None, sections=None, downtime=None,
                 pulses=None, window=None, begin=None, end=None, **kwargs):
        result = allocate_pulses(
            total, shifts=shifts, sections=sections, downtime=downtime,
            pulses=pulses, window=window, begin=begin, end=end, **kwargs
        )
        self.total = result.total
        self.window = result.window
        self.shift_totals = result.shift_totals
        self.section_totals = result.section_totals
        self.entries = result.entries
        self.assignments = result.assignments
        self.remainders = result.remainders


Ledger = PulseLedger
PulseAllocation = Allocation
