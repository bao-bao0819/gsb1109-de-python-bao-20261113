"""
Tests for tracing recycled labels back to physical batches.

The lending history used throughout:

    batch A: [0, 10)
    batch B: [10, 20)
    gap:     [20, 30)   # no lending record at all
    batch C: [30, 40)
"""
from __future__ import absolute_import

from datetime import datetime, timedelta

import pytest

from intervaltree import UNCOVERED, BatchHistory, build_history


@pytest.fixture
def history():
    return BatchHistory([
        (0, 10, 'A'),
        (10, 20, 'B'),
        (30, 40, 'C'),
    ])


# ---------------------------------------------------------------------------
# Construction and validation
# ---------------------------------------------------------------------------

def test_build_history_helper():
    history = build_history([(0, 10, 'A')])
    assert history.candidates(2, 8) == ('A',)


def test_rejects_empty_or_reversed_lending_interval():
    with pytest.raises(ValueError):
        BatchHistory([(5, 5, 'A')])
    with pytest.raises(ValueError):
        BatchHistory([(8, 3, 'A')])


def test_rejects_unsorted_records():
    with pytest.raises(ValueError):
        BatchHistory([(10, 20, 'B'), (0, 5, 'A')])


def test_rejects_overlapping_intervals_for_different_batches():
    with pytest.raises(ValueError):
        BatchHistory([(0, 5, 'A'), (3, 8, 'B')])


def test_overlapping_intervals_for_same_batch_are_allowed():
    history = BatchHistory([(0, 15, 'A'), (5, 20, 'A')])
    assert history.candidates(2, 18) == ('A',)


def test_rejects_malformed_record():
    with pytest.raises(ValueError):
        BatchHistory([(0, 5)])


# ---------------------------------------------------------------------------
# Single scan attribution
# ---------------------------------------------------------------------------

def test_single_scan_inside_one_batch(history):
    assert history.candidates(2, 5) == ('A',)
    assert history.trace_scan(2, 5) == 'A'


def test_window_crossing_batch_boundary_offers_both(history):
    assert history.candidates(8, 12) == ('A', 'B')
    assert history.trace_scan(8, 12) == ('A', 'B')


def test_half_open_boundary_excludes_previous_batch(history):
    # A ends at 10 and 10 itself is not part of A.
    assert history.candidates(10, 12) == ('B',)
    assert history.candidates(9, 10) == ('A',)


def test_window_in_gap_is_uncovered_not_previous_batch(history):
    assert history.candidates(22, 25) == ()
    assert history.trace_scan(22, 25) is UNCOVERED
    assert not bool(UNCOVERED)
    assert repr(UNCOVERED) == 'UNCOVERED'


def test_empty_history_is_uncovered():
    history = BatchHistory()
    assert history.candidates(0, 1) == ()
    assert history.trace_scan(0, 1) is UNCOVERED


def test_window_partly_in_gap_reports_the_gap(history):
    # [18, 22): B covers [18, 20), [20, 22) has no record.
    assert history.candidates(18, 22) == ('B',)
    assert history.uncovered_spans(18, 22) == ((20, 22),)


def test_uncovered_spans_inside_and_across_gap(history):
    assert history.uncovered_spans(2, 25) == ((20, 25),)
    assert history.uncovered_spans(22, 35) == ((22, 30),)
    assert history.uncovered_spans(2, 8) == ()


def test_invalid_scan_window_raises(history):
    with pytest.raises(ValueError):
        history.candidates(5, 5)
    with pytest.raises(ValueError):
        history.trace_scan(9, 3)


# ---------------------------------------------------------------------------
# Ordered scan sequences sharing one lending history
# ---------------------------------------------------------------------------

def test_sequence_with_unique_attribution(history):
    assert history.trace_sequence([(2, 5), (12, 15)]) == (
        ('A', 'B'),
    )


def test_sequence_returns_two_plausible_attributions(history):
    # First scan straddles the A/B boundary; the second can only be B.
    assert history.trace_sequence([(8, 12), (15, 18)]) == (
        ('A', 'B'),
        ('B', 'B'),
    )


def test_sequence_does_not_pick_nearest_batch_per_scan(history):
    # Both windows straddle the A/B boundary. Picking B then A looks
    # possible window by window, but no increasing pair of timestamps can
    # support it, so the shared-history check must reject that path.
    results = history.trace_sequence([(8, 12), (9, 11)], limit=10)
    assert ('B', 'A') not in results
    assert ('A', 'A') in results
    assert ('A', 'B') in results
    assert ('B', 'B') in results
    assert len(results) == 3


def test_sequence_respects_limit(history):
    results = history.trace_sequence([(8, 12), (9, 11)], limit=2)
    assert len(results) == 2


def test_sequence_keeps_uncovered_positions_explicit(history):
    assert history.trace_sequence([(5, 7), (22, 25), (32, 35)]) == (
        ('A', UNCOVERED, 'C'),
    )


def test_sequence_fully_inside_gap_is_uncovered(history):
    assert history.trace_sequence([(22, 24), (25, 27)]) == (
        (UNCOVERED, UNCOVERED),
    )


def test_sequence_mixing_batch_and_uncovered_window(history):
    assert history.trace_sequence([(18, 20), (22, 25)]) == (
        ('B', UNCOVERED),
    )


def test_sequence_on_empty_history():
    history = BatchHistory()
    assert history.trace_sequence([(0, 1), (2, 3)]) == (
        (UNCOVERED, UNCOVERED),
    )


def test_sequence_rejects_out_of_order_windows(history):
    with pytest.raises(ValueError):
        history.trace_sequence([(10, 12), (8, 9)])


def test_sequence_rejects_bad_window_and_limit(history):
    with pytest.raises(ValueError):
        history.trace_sequence([(5, 5)])
    with pytest.raises(ValueError):
        history.trace_sequence([(2, 5)], limit=0)


def test_sequence_with_datetime_windows():
    base = datetime(2026, 11, 1)
    history = BatchHistory([
        (base, base + timedelta(hours=10), 'A'),
        (base + timedelta(hours=10), base + timedelta(hours=20), 'B'),
    ])
    results = history.trace_sequence([
        (base + timedelta(hours=8), base + timedelta(hours=12)),
        (base + timedelta(hours=15), base + timedelta(hours=18)),
    ])
    assert results == (('A', 'B'), ('B', 'B'))


def test_same_batch_lent_twice_keeps_one_attribution(history):
    # Add a second, later lending interval for an existing batch.
    history = BatchHistory([
        (0, 10, 'A'),
        (10, 20, 'B'),
        (40, 50, 'A'),
    ])
    assert history.trace_sequence([(2, 5), (42, 45)]) == (
        ('A', 'A'),
    )
