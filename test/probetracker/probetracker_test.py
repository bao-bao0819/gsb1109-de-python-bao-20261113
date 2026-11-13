"""
intervaltree: A mutable, self-balancing interval tree for Python 2 and 3.
Queries may be by point, by range overlap, or by range envelopment.

Test module: ProbeTracker
"""
from __future__ import absolute_import
from datetime import datetime, timedelta

import pytest

from intervaltree import Interval, ProbeTracker


def test_empty_tracker():
    t = ProbeTracker()
    assert len(t) == 0
    assert t.online_at(5) == set()
    assert t.peak_online() == (0, [])
    assert t.peak_online(0, 900) == (0, [])
    assert Interval(0, 10, 'a') not in t


def test_distinct_probes_overlapping():
    t = ProbeTracker()
    t.addi(0, 10, 'a')
    t.addi(5, 15, 'b')
    count, periods = t.peak_online()
    assert count == 2
    assert periods == [Interval(5, 10)]
    assert t.online_at(7) == set(['a', 'b'])
    assert t.online_at(12) == set(['b'])


def test_same_probe_overlapping_heartbeats_count_once():
    t = ProbeTracker()
    t.addi(0, 10, 'a')
    t.addi(3, 8, 'a')
    t.addi(5, 20, 'a')
    count, periods = t.peak_online()
    assert count == 1
    assert periods == [Interval(0, 20)]


def test_duplicate_reference_revocation():
    t = ProbeTracker()
    t.addi(0, 10, 'a')
    t.addi(0, 10, 'a')  # same heartbeat referenced twice
    assert len(t) == 1
    assert t.reference_count(Interval(0, 10, 'a')) == 2

    # Revoking one of two references keeps the coverage.
    t.revokei(0, 10, 'a')
    assert t.online_at(5) == set(['a'])
    assert t.peak_online() == (1, [Interval(0, 10)])

    # Revoking the last reference drops the coverage.
    t.revokei(0, 10, 'a')
    assert t.peak_online() == (0, [])
    assert Interval(0, 10, 'a') not in t

    # No reference left to revoke.
    with pytest.raises(ValueError):
        t.revokei(0, 10, 'a')
    # discard stays quiet.
    t.discardi(0, 10, 'a')


def test_handoff_no_instantaneous_peak():
    t = ProbeTracker()
    t.addi(0, 10, 'a')
    t.addi(10, 20, 'b')  # b comes online exactly as a goes offline
    count, periods = t.peak_online()
    assert count == 1
    assert periods == [Interval(0, 20)]
    assert t.online_at(10) == set(['b'])


def test_window_scoping_and_clipping():
    t = ProbeTracker()
    t.addi(0, 100, 'a')
    t.addi(50, 60, 'b')

    # A 15-minute window missing b's stint.
    assert t.peak_online(0, 15) == (1, [Interval(0, 15)])
    # A window clipping the overlap from both sides.
    assert t.peak_online(45, 65) == (2, [Interval(50, 60)])
    # A window with no coverage at all.
    assert t.peak_online(200, 215) == (0, [])
    # An empty window.
    assert t.peak_online(10, 10) == (0, [])


def test_multiple_peak_periods():
    t = ProbeTracker()
    t.addi(0, 10, 'a')
    t.addi(2, 5, 'b')
    t.addi(20, 30, 'c')
    t.addi(25, 28, 'd')
    count, periods = t.peak_online()
    assert count == 2
    assert periods == [Interval(2, 5), Interval(25, 28)]


def test_peak_periods_merge_across_probe_change():
    # The probe set changes at 10, but the count stays at the peak, so
    # the two segments merge into one maximal period.
    t = ProbeTracker()
    t.addi(0, 10, 'a')
    t.addi(0, 10, 'b')
    t.addi(10, 20, 'c')
    t.addi(10, 20, 'd')
    count, periods = t.peak_online()
    assert count == 2
    assert periods == [Interval(0, 20)]


def test_datetime_window():
    base = datetime(2026, 11, 14, 12, 0, 0)
    t = ProbeTracker()
    t.addi(base, base + timedelta(hours=1), 'a')
    t.addi(base + timedelta(minutes=10), base + timedelta(minutes=20), 'b')
    count, periods = t.peak_online(base, base + timedelta(minutes=15))
    assert count == 2
    assert periods == [Interval(base + timedelta(minutes=10),
                               base + timedelta(minutes=15))]


def test_mutation_api():
    t = ProbeTracker.from_tuples([(0, 10, 'a'), (5, 15, 'b')])
    assert len(t) == 2
    t.add(Interval(8, 12, 'c'))
    assert t.peak_online() == (3, [Interval(8, 10)])
    t.remove(Interval(8, 12, 'c'))
    assert t.peak_online() == (2, [Interval(5, 10)])
    t.discard(Interval(0, 10, 'a'))
    assert t.peak_online() == (1, [Interval(5, 15)])
    with pytest.raises(ValueError):
        t.addi(5, 5, 'x')  # null interval rejected
