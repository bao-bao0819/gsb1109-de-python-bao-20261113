#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
intervaltree: A mutable, self-balancing interval tree for Python 2 and 3.
Queries may be by point, by range overlap, or by range envelopment.

ProbeTracker: peak concurrent-probe queries over heartbeat intervals.
"""
from collections import Counter, defaultdict

from .interval import Interval
from .intervaltree import IntervalTree


class ProbeTracker(object):
    """
    Tracks probe online/offline intervals (heartbeats) and answers
    peak-concurrency queries.

    Each probe is identified by the ``data`` field of its intervals.
    Intervals are half-open, ``[begin, end)``: a probe going offline at
    ``t`` and another coming online at ``t`` are never online at the
    same instant, so a handoff cannot create an instantaneous peak.

    Overlapping heartbeats of the same probe count as one online probe.

    The same heartbeat may be recorded more than once. Each ``add()``
    is a reference; ``remove()``/``revoke()`` drops a single reference,
    and the interval's coverage only lapses when its last reference is
    revoked. This makes late-arriving revocations safe against
    duplicate delivery.

    Examples:
    ---------

        >>> tracker = ProbeTracker()
        >>> tracker.addi(0, 10, 'probe-a')
        >>> tracker.addi(5, 15, 'probe-b')
        >>> tracker.peak_online()
        (2, [Interval(5, 10)])

    Overlapping heartbeats of one probe count once::

        >>> tracker = ProbeTracker()
        >>> tracker.addi(0, 10, 'probe-a')
        >>> tracker.addi(3, 8, 'probe-a')
        >>> tracker.peak_online()
        (1, [Interval(0, 10)])

    A handoff creates no instantaneous peak::

        >>> tracker = ProbeTracker()
        >>> tracker.addi(0, 10, 'probe-a')
        >>> tracker.addi(10, 20, 'probe-b')
        >>> tracker.peak_online()
        (1, [Interval(0, 20)])

    A heartbeat referenced twice survives a single revocation::

        >>> tracker = ProbeTracker()
        >>> tracker.addi(0, 10, 'probe-a')
        >>> tracker.addi(0, 10, 'probe-a')
        >>> tracker.revokei(0, 10, 'probe-a')
        >>> tracker.peak_online()
        (1, [Interval(0, 10)])
        >>> tracker.revokei(0, 10, 'probe-a')
        >>> tracker.peak_online()
        (0, [])
    """

    def __init__(self, intervals=None):
        """
        Set up an empty tracker, optionally seeded with an iterable of
        Intervals whose data fields identify the probes.
        """
        self._tree = IntervalTree()
        self._refs = Counter()
        if intervals is not None:
            self.update(intervals)

    @classmethod
    def from_tuples(cls, tups):
        """
        Create a new ProbeTracker from an iterable of 2- or 3-tuples,
        where the tuple lists begin, end, and optionally the probe id.
        """
        return cls(Interval(*t) for t in tups)

    def add(self, interval):
        """
        Adds a reference to an online interval. If an equal interval
        (same range and probe) is already present, only its reference
        count is increased.

        Completes in O(log n) time.
        """
        if interval.is_null():
            raise ValueError(
                "ProbeTracker: Null Interval objects not allowed:"
                " {0}".format(interval)
            )
        self._refs[interval] += 1
        if self._refs[interval] == 1:
            self._tree.add(interval)
    append = add

    def addi(self, begin, end, probe=None):
        """
        Shortcut for add(Interval(begin, end, probe)).

        Completes in O(log n) time.
        """
        return self.add(Interval(begin, end, probe))
    appendi = addi

    def update(self, intervals):
        """
        Given an iterable of intervals, add a reference to each.

        Completes in O(m*log(n+m)), where m = number of intervals to
        add.
        """
        for iv in intervals:
            self.add(iv)

    def remove(self, interval):
        """
        Revokes one reference to an interval. The interval's coverage
        lapses only when its last reference is revoked. If no reference
        is present, raises ValueError.

        Completes in O(log n) time.
        """
        if interval not in self._refs:
            raise ValueError
        self._refs[interval] -= 1
        if self._refs[interval] <= 0:
            del self._refs[interval]
            self._tree.remove(interval)
    revoke = remove

    def removei(self, begin, end, probe=None):
        """
        Shortcut for remove(Interval(begin, end, probe)).

        Completes in O(log n) time.
        """
        return self.remove(Interval(begin, end, probe))
    revokei = removei

    def discard(self, interval):
        """
        Revokes one reference to an interval, if present; otherwise
        does nothing.

        Completes in O(log n) time.
        """
        if interval in self._refs:
            self.remove(interval)

    def discardi(self, begin, end, probe=None):
        """
        Shortcut for discard(Interval(begin, end, probe)).

        Completes in O(log n) time.
        """
        return self.discard(Interval(begin, end, probe))

    def reference_count(self, interval):
        """
        Returns how many live references the interval has.

        Completes in O(1) time.
        :rtype: int
        """
        return self._refs[interval]

    def online_at(self, point):
        """
        Returns the set of distinct probes online at the given point.

        Completes in O(m + log n) time, where m = number of matches.
        :rtype: set
        """
        return set(iv.data for iv in self._tree.at(point))

    def peak_online(self, begin=None, end=None):
        """
        Within the query window [begin, end) -- for example, any
        15-minute span -- finds the maximum number of distinct probes
        online at a single instant, and all maximal periods within the
        window where that number is sustained.

        If begin or end is None, the tracker's own bounds are used.
        Intervals are clipped to the window. If no probe is online in
        the window, returns (0, []).

        Completes in O(m + k*log n + k*log k) time, where:
          * n = number of distinct intervals tracked
          * m = number of intervals overlapping the window
          * k = number of interval boundaries inside the window

        :rtype: (int, list of Interval)
        """
        if self._tree.is_empty():
            return 0, []
        if begin is None:
            begin = self._tree.begin()
        if end is None:
            end = self._tree.end()
        if begin >= end:
            return 0, []

        starts = defaultdict(Counter)
        stops = defaultdict(Counter)
        for iv in self._tree.overlap(begin, end):
            starts[max(iv.begin, begin)][iv.data] += 1
            stops[min(iv.end, end)][iv.data] += 1

        # Sweep the boundaries. At each coordinate, intervals ending
        # there no longer cover it, while intervals beginning there do
        # (half-open ranges), so a handoff never double-counts.
        active = Counter()
        count = 0
        prev = None
        segments = []  # (start, end, distinct online probes), count > 0
        for x in sorted(set(starts) | set(stops)):
            if prev is not None and prev < x and count:
                segments.append((prev, x, count))
            for probe, n in stops[x].items():
                active[probe] -= n
                if active[probe] <= 0:
                    del active[probe]
            for probe, n in starts[x].items():
                active[probe] += n
            count = len(active)
            prev = x

        if not segments:
            return 0, []
        best = max(seg[2] for seg in segments)

        # Merge touching segments at the peak count into maximal periods.
        periods = []
        for seg_begin, seg_end, seg_count in segments:
            if seg_count != best:
                continue
            if periods and periods[-1].end == seg_begin:
                periods[-1] = Interval(periods[-1].begin, seg_end)
            else:
                periods.append(Interval(seg_begin, seg_end))
        return best, periods

    def __contains__(self, interval):
        """
        Whether at least one reference to the interval is present.

        Completes in O(1) time.
        :rtype: bool
        """
        return interval in self._refs

    def __len__(self):
        """
        Returns how many distinct intervals are tracked (regardless of
        their reference counts).

        Completes in O(1) time.
        :rtype: int
        """
        return len(self._tree)

    def __repr__(self):
        """
        :rtype: str
        """
        ivs = sorted(self._tree)
        if not ivs:
            return "{0}()".format(self.__class__.__name__)
        else:
            return "{0}({1})".format(self.__class__.__name__, ivs)

    __str__ = __repr__
