#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Trace recycled material labels back to physical batches.

A physical label is reused across several physical batches. For each batch
that carried the label, we only know the valid interval during which the
label was lent out: [checkout, return). A scan itself has no exact
timestamp; we only know the time range [earliest, latest) during which it
could have happened at a given workstation.

An IntervalTree indexes the lending intervals; each interval's data is the
physical batch that held the label while the interval was in effect.

The module answers two questions:

* Which batches could a single scan belong to?
* Given several scans known to have happened in order, which complete
  lending histories are consistent with every scan?

Intervals are half-open, matching the library convention: a point t is
covered when begin <= t < end. Time coordinates are treated as a dense
order (as with datetime or float timestamps), so a window ending just
after an earlier scan time still admits a later point inside it.
"""
from __future__ import absolute_import

from intervaltree import IntervalTree


class _Uncovered(object):
    """Sentinel for scan windows that no lending interval covers."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(_Uncovered, cls).__new__(cls)
        return cls._instance

    def __repr__(self):
        return 'UNCOVERED'

    def __bool__(self):
        return False

    __nonzero__ = __bool__


#: A scan window that no batch's lending interval covers. It is never
#: collapsed into the previous batch: callers must treat it as missing
#: history.
UNCOVERED = _Uncovered()


class BatchHistory(object):
    """
    Lending history of one recycled label.

    :param records: iterable of ``(checkout, return, batch)`` tuples. Each
        says the label was physically attached to ``batch`` from
        ``checkout`` (inclusive) until ``return`` (exclusive).
    :raises ValueError: if a record is not a valid half-open interval, if
        two records claim different batches at the same time, or if records
        are not given in chronological order.
    """

    def __init__(self, records=()):
        self.tree = IntervalTree()
        self._add_records(records)

    def _add_records(self, records):
        last_begin = None
        for record in records:
            try:
                begin, end, batch = record
            except (TypeError, ValueError):
                raise ValueError(
                    'each record must be a (checkout, return, batch) tuple, '
                    'got {!r}'.format(record))
            if not begin < end:
                raise ValueError(
                    'lending interval for batch {!r} must satisfy '
                    'checkout < return: {!r}'.format(batch, (begin, end)))
            if last_begin is not None and begin < last_begin:
                raise ValueError(
                    'records must be in chronological order; {!r} starts '
                    'before an earlier record'.format(batch))
            last_begin = begin
            overlapping = self.tree.overlap(begin, end)
            for interval in overlapping:
                if interval.data != batch:
                    raise ValueError(
                        'overlapping lending intervals for different '
                        'batches: {!r} conflicts with {!r}'.format(
                            (begin, end, batch),
                            (interval.begin, interval.end, interval.data)))
            self.tree.addi(begin, end, batch)

    def candidates(self, window_lo, window_hi):
        """
        Return every batch whose lending interval could contain a scan.

        A batch qualifies when its interval overlaps the whole uncertainty
        window [window_lo, window_hi), i.e. there is at least one moment in
        the window at which the label was lent out to that batch.

        :return: sorted tuple of batches. Empty means the window falls in a
            gap with no lending record; callers should treat that as
            :data:`UNCOVERED`, never as the previous batch.
        :raises ValueError: if the window is empty or reversed.
        """
        if not window_lo < window_hi:
            raise ValueError(
                'scan window must satisfy lo < hi: {!r}'.format(
                    (window_lo, window_hi)))
        batches = set()
        for interval in self.tree.overlap(window_lo, window_hi):
            batches.add(interval.data)
        return tuple(sorted(batches))

    def uncovered_spans(self, window_lo, window_hi):
        """
        Return the sub-ranges of a scan window with no lending record.

        :return: sorted tuple of ``(begin, end)`` gap intervals. A
            non-empty result means part of the window cannot be attributed
            to any batch.
        """
        if not window_lo < window_hi:
            raise ValueError(
                'scan window must satisfy lo < hi: {!r}'.format(
                    (window_lo, window_hi)))
        covering = sorted(
            (interval.begin, interval.end)
            for interval in self.tree.overlap(window_lo, window_hi))
        gaps = []
        cursor = window_lo
        for begin, end in covering:
            if begin > cursor:
                gaps.append((cursor, begin))
            if end > cursor:
                cursor = end
            if cursor >= window_hi:
                break
        if cursor < window_hi:
            gaps.append((cursor, window_hi))
        return tuple(gaps)

    def trace_scan(self, window_lo, window_hi):
        """
        Attribute a single scan.

        :return:
            * a single batch if exactly one fits,
            * a sorted tuple of batches if several fit,
            * :data:`UNCOVERED` if no lending interval reaches the window.
        """
        candidates = self.candidates(window_lo, window_hi)
        if not candidates:
            return UNCOVERED
        if len(candidates) == 1:
            return candidates[0]
        return candidates

    def _intervals_for(self, batch):
        return [
            interval for interval in self.tree
            if interval.data == batch]

    @staticmethod
    def _earliest_feasible(lo, hi, intervals, after):
        """
        Infimum of the timestamps in [lo, hi) covered by one of
        ``intervals`` for which a point strictly greater than ``after``
        exists. When ``after`` itself is the infimum, a feasible strictly
        greater point still exists as long as the interval extends past
        it. Returns None when no timestamp can follow ``after``.
        """
        best = None
        for interval in intervals:
            lower = max(lo, interval.begin)
            upper = min(hi, interval.end)
            if lower >= upper:
                continue
            if after is not None and upper <= after:
                continue
            if after is not None and lower <= after:
                lower = after
            if best is None or lower < best:
                best = lower
        return best

    def trace_sequence(self, scans, limit=2):
        """
        Attribute an ordered sequence of scans under one shared history.

        Every scan must be explainable by the same lending history: a
        complete attribution is returned only when there exist ordered
        timestamps, one inside each scan window and its chosen batch's
        lending interval, so candidates are never picked independently by
        nearest time.

        :param scans: ordered iterable of ``(window_lo, window_hi)``
            uncertainty windows; each window is the possible time range of
            one scan at its workstation. Window starts must be
            non-decreasing.
        :param limit: maximum number of complete attributions to return.
        :return: a tuple of attributions, each a tuple with one entry per
            scan that is either a batch or :data:`UNCOVERED`. Two
            attributions are returned whenever more than one history is
            consistent with the windows.
        :raises ValueError: for an empty/reversed window, an out-of-order
            sequence, or a non-positive ``limit``.
        """
        windows = tuple(scans)
        if limit < 1:
            raise ValueError('limit must be positive, got {!r}'.format(limit))
        options_per_scan = []
        previous_lo = None
        for window in windows:
            try:
                lo, hi = window
            except (TypeError, ValueError):
                raise ValueError(
                    'each scan must be a (lo, hi) window, got {!r}'.format(
                        window))
            if not lo < hi:
                raise ValueError(
                    'scan window must satisfy lo < hi: {!r}'.format((lo, hi)))
            if previous_lo is not None and lo < previous_lo:
                raise ValueError(
                    'scans must be in chronological order; window {!r} '
                    'starts before an earlier window'.format((lo, hi)))
            previous_lo = lo
            candidates = self.candidates(lo, hi)
            options_per_scan.append(
                candidates if candidates else (UNCOVERED,))

        interval_cache = {}
        results = []

        def walk(position, path, after):
            if len(results) >= limit:
                return
            if position == len(windows):
                results.append(tuple(path))
                return
            lo, hi = windows[position]
            for option in options_per_scan[position]:
                if option is UNCOVERED:
                    walk(position + 1, path + [option], after)
                    continue
                intervals = interval_cache.setdefault(
                    option, self._intervals_for(option))
                chosen = self._earliest_feasible(lo, hi, intervals, after)
                if chosen is None:
                    continue
                walk(position + 1, path + [option], chosen)

        walk(0, [], None)
        return tuple(results)


def build_history(records):
    """
    Build a :class:`BatchHistory` from lending records.

    :param records: iterable of ``(checkout, return, batch)`` tuples.
    """
    return BatchHistory(records)
