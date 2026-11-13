"""
Test module: integer pulse allocation across nested, half-open segments.
"""

from __future__ import absolute_import

from datetime import datetime, timedelta
from fractions import Fraction

import pytest

from intervaltree import (
    Allocation,
    AllocationError,
    AmbiguousPulseError,
    Interval,
    InvalidLedgerError,
    Pulse,
    PulseLedger,
    UnallocatedPulseError,
    allocate_pulses,
)


def _sum(totals):
    return sum(totals.values())


def test_equal_shifts_split_evenly():
    result = allocate_pulses(10, {'A': (0, 5), 'B': (5, 10)})
    assert result.total == 10
    assert result.shift_totals == {'A': 5, 'B': 5}
    assert result.check()


def test_uneven_shifts_use_largest_remainder():
    result = allocate_pulses(7, {'A': (0, 3), 'B': (3, 10)})
    assert result.by_shift['A'] == 2
    assert result.by_shift['B'] == 5
    entry_a, entry_b = result.shift_entries
    assert entry_a.exact == Fraction(21, 10)
    assert entry_a.fraction == Fraction(1, 10)
    assert entry_a.extra == 0
    assert entry_b.extra == 1
    assert entry_b.rank == 1


def test_cut_point_belongs_to_the_later_segment():
    result = allocate_pulses(
        3, {'early': (0, 5), 'late': (5, 10)}, pulses=[5]
    )
    assignment = result.assignments[0]
    assert assignment.timestamp == 5
    assert assignment.shift == 'late'
    # One known pulse is exact; the remaining two split 1/1.
    assert result.by_shift == {'early': 1, 'late': 2}


def test_known_pulses_are_assigned_before_apportionment():
    result = allocate_pulses(
        12,
        {'A': (0, 10), 'B': (10, 20)},
        pulses=[3, {'time': 14, 'count': 2}, Pulse(7)],
    )
    # A and B each get 2 exact pulses, leaving 8 to split 4/4.
    assert result.by_shift['A'] == 6
    assert result.by_shift['B'] == 6
    assert [a.shift for a in result.assignments] == ['A', 'B', 'A']
    assert result.assignments[1].count == 2


def test_downtime_gets_no_share_of_the_remainder():
    result = allocate_pulses(
        11,
        {'A': (0, 10), 'B': (10, 20)},
        downtime=[(3, 9)],
    )
    # Effective durations are 4 and 10.
    assert result.by_shift['A'] == 3
    assert result.by_shift['B'] == 8
    assert result.check()


def test_downtime_ranges_are_merged_before_subtraction():
    result = allocate_pulses(
        6,
        {'A': (0, 5), 'B': (5, 10)},
        downtime=[(2, 7), (6, 8)],
    )
    # Merged downtime [2, 8) leaves 2 effective hours in each shift.
    assert result.by_shift['A'] == 3
    assert result.by_shift['B'] == 3


def test_sections_sum_to_the_shift_total():
    result = allocate_pulses(
        9,
        {'A': (0, 10), 'B': (10, 20)},
        sections={
            'A': {'A1': (0, 4), 'A2': (4, 10)},
            'B': {'B1': (10, 15), 'B2': (15, 20)},
        },
    )
    assert result.by_shift['A'] == 5
    assert result.by_shift['B'] == 4
    assert result.by_section['A']['A1'] == 2
    assert result.by_section['A']['A2'] == 3
    assert result.by_section['B']['B1'] == 2
    assert result.by_section['B']['B2'] == 2
    assert _sum(result.by_shift) == result.total
    assert result.check()


def test_nested_totals_are_recheckable_from_entries():
    result = allocate_pulses(
        17,
        {'A': (0, 10), 'B': (10, 20)},
        sections=[('A', 'A1', 0, 6), ('A', 'A2', 6, 10),
                  ('B', 'B1', 10, 13), ('B', 'B2', 13, 20)],
        downtime=[(4, 5)],
        pulses={12: 3},
    )
    assert _sum(result.by_shift) == 17
    for shift_key, shift_total in result.by_shift.items():
        assert _sum(result.by_section[shift_key]) == shift_total
    assert sum(entry.total for entry in result.section_entries) == 17
    assert result.shift_totals == {'A': 7, 'B': 10}
    assert result.section_totals['A'] == {'A1': 4, 'A2': 3}
    assert result.check()


def test_remainder_awards_are_traceable_to_the_entries():
    result = allocate_pulses(
        5,
        [('A', 0, 3), ('B', 3, 5), ('C', 5, 10)],
    )
    assert result.by_shift == {'A': 2, 'B': 1, 'C': 2}
    floors = sum(entry.floor for entry in result.shift_entries)
    extras = sum(entry.extra for entry in result.shift_entries)
    assert floors + extras == 5
    assert extras == 1
    assert result.remainders == [
        [entry for entry in result.shift_entries if entry.key == 'A'][0]
    ]


def test_largest_remainder_ties_are_auditable():
    result = allocate_pulses(
        5, [('A', 0, 3), ('B', 3, 5), ('C', 5, 10)]
    )
    # Exact shares: A 1.5, B 1.0, C 2.5. Floors: 1, 1, 2. One pulse
    # remains; it goes to A because the fractional parts tie and A
    # begins first.
    ranks = dict((e.key, e.rank) for e in result.shift_entries)
    assert ranks == {'A': 1, 'C': 2, 'B': 3}
    assert result.by_shift == {'A': 2, 'B': 1, 'C': 2}


def test_explicit_shifts_without_sections_get_implicit_section():
    result = allocate_pulses(4, {'A': (0, 2), 'B': (2, 4)})
    assert result.section_totals['A'] == {'A': 2}
    assert result.section_entries[0].implicit


def test_sections_can_be_clipped_to_their_shift():
    result = allocate_pulses(
        8,
        {'A': (0, 10), 'B': (10, 20)},
        sections=[
            {'shift': 'A', 'section': 'A1', 'begin': -5, 'end': 2},
            {'shift': 'A', 'section': 'A2', 'begin': 2, 'end': 15},
            {'shift': 'B', 'section': 'B1', 'begin': 5, 'end': 25},
        ],
    )
    assert _sum(result.by_shift) == 8
    for shift_key, shift_total in result.by_shift.items():
        assert _sum(result.by_section[shift_key]) == shift_total
    durations = dict((e.key, e.duration) for e in result.section_entries)
    assert durations == {'A1': 2, 'A2': 8, 'B1': 10}


def test_datetime_window_and_downtime():
    base = datetime(2026, 11, 13)
    window = (base, base + timedelta(hours=8))
    shifts = {
        'day': (base, base + timedelta(hours=8)),
    }
    sections = {
        'day': {
            'setup': (base, base + timedelta(hours=2)),
            'run': (base + timedelta(hours=2), base + timedelta(hours=8)),
        }
    }
    result = allocate_pulses(
        60, shifts, sections=sections, window=window,
        downtime=[(base + timedelta(hours=4),
                   base + timedelta(hours=5))],
        pulses=[base + timedelta(hours=8) - timedelta(seconds=1)],
    )
    assert result.by_shift['day'] == 60
    # Setup keeps 2 effective hours; run keeps 5 of its 6.
    assert result.by_section['day']['setup'] == 17
    assert result.by_section['day']['run'] == 43
    assert result.assignments[0].section == 'run'


def test_interval_objects_are_accepted_as_periods():
    result = allocate_pulses(
        6,
        [Interval(0, 5, 'A'), Interval(5, 10, 'B')],
    )
    assert result.by_shift == {'A': 3, 'B': 3}


def test_known_pulse_in_zero_effective_duration_section_is_still_exact():
    result = allocate_pulses(
        10,
        {'A': (0, 10), 'B': (10, 20)},
        sections={'A': {'idle': (0, 2), 'work': (2, 10)}},
        downtime=[(2, 10)],
        pulses=[1],
    )
    assert result.by_section['A']['idle'] == 3
    assert result.by_section['A']['work'] == 0


def test_single_segment_with_zero_duration_holds_all_pulses():
    result = allocate_pulses(
        5, {'A': (0, 10)}, downtime=[(0, 10)]
    )
    assert result.by_shift['A'] == 5


def test_zero_duration_with_multiple_segments_is_an_error():
    with pytest.raises(AllocationError):
        allocate_pulses(
            5,
            {'A': (0, 10), 'B': (10, 20)},
            downtime=[(0, 10), (10, 20)],
        )


def test_known_pulse_count_cannot_exceed_total():
    with pytest.raises(AllocationError):
        allocate_pulses(
            2,
            {'A': (0, 10), 'B': (10, 20)},
            pulses=[3, 12, {'time': 4, 'count': 2}],
        )


def test_known_pulse_outside_the_window_is_rejected():
    with pytest.raises(UnallocatedPulseError):
        allocate_pulses(
            5, {'A': (0, 10)}, window=(0, 10), pulses=[10]
        )


def test_known_pulse_in_a_gap_is_rejected():
    with pytest.raises(UnallocatedPulseError):
        allocate_pulses(
            5, {'A': (0, 5), 'B': (5, 10)}, pulses=[15]
        )


def test_ambiguous_overlapping_shifts_are_rejected_for_known_pulses():
    with pytest.raises(AmbiguousPulseError):
        allocate_pulses(
            5,
            {'A': (0, 8), 'B': (2, 10)},
            pulses=[5],
        )


def test_negative_total_is_rejected():
    with pytest.raises(InvalidLedgerError):
        allocate_pulses(-1, {'A': (0, 1)})


def test_duplicate_shift_keys_are_rejected():
    with pytest.raises(InvalidLedgerError):
        allocate_pulses(
            5, [('A', 0, 5), ('A', 5, 10)]
        )


def test_empty_range_is_rejected():
    with pytest.raises(InvalidLedgerError):
        allocate_pulses(5, {'A': (5, 5)})


def test_pulse_ledger_class_and_aliases():
    ledger = PulseLedger(
        10, {'A': (0, 4), 'B': (4, 10)}, pulses=(6, 1)
    )
    assert isinstance(ledger, Allocation)
    assert ledger.master_total == 10
    assert ledger.by_shift()['A'] == 4
    assert ledger.by_shift()['B'] == 6
    assert _sum(ledger.by_section['A']) == 4
    assert ledger.summary()['total'] == 10


def test_allocation_summary_reports_remainders_and_known_pulses():
    result = allocate_pulses(
        5,
        [('A', 0, 3), ('B', 3, 5), ('C', 5, 10)],
        pulses=[4],
    )
    summary = result.summary()
    assert summary['total'] == 5
    assert summary['known_pulses'][0]['shift'] == 'B'
    assert summary['remainders']
    for row in summary['remainders']:
        assert row['remainder'] >= 1
        assert row['rank'] >= 1


def test_intervaltree_input_is_supported_and_order_independent():
    from intervaltree import IntervalTree

    tree = IntervalTree([Interval(0, 3, 'A'), Interval(3, 5, 'B'),
                         Interval(5, 10, 'C')])
    first = allocate_pulses(5, tree)
    second = allocate_pulses(5, set(tree))
    assert dict(first.by_shift) == dict(second.by_shift)
    assert first.by_shift == {'A': 2, 'B': 1, 'C': 2}


def test_single_period_shorthand():
    result = allocate_pulses(9, {'key': 'day', 'begin': 0, 'end': 10})
    assert result.by_shift == {'day': 9}


def test_known_pulse_audit_records_the_half_open_rule():
    result = allocate_pulses(
        2, {'early': (0, 5), 'late': (5, 10)}, pulses=[5]
    )
    row = result.assignments[0].as_dict()
    assert row['rule'] == 'half-open containment'
    assert row['shift'] == 'late'
    assert row['section'] == 'late'


def test_entry_audit_fields_expose_the_full_remainder_math():
    result = allocate_pulses(
        5, [('A', 0, 3), ('B', 3, 5), ('C', 5, 10)]
    )
    entry = [e for e in result.shift_entries if e.key == 'A'][0]
    assert entry.quota == 1.5
    assert entry.floor == 1
    assert entry.fraction == Fraction(1, 2)
    assert entry.extra == 1
    assert entry.total == 2
    assert entry.apportioned == 2
    assert entry.got_remainder
    assert result.award_rule.startswith('largest-remainder')


def test_check_detects_a_tampered_total():
    result = allocate_pulses(4, {'A': (0, 2), 'B': (2, 4)})
    result.shift_totals['A'] = 3
    with pytest.raises(AllocationError):
        result.check()


def test_single_pulse_shorthand_and_downtime_interval():
    result = allocate_pulses(
        5, {'A': (0, 5), 'B': (5, 10)}, pulses=5,
        downtime=Interval(1, 2),
    )
    assert len(result.assignments) == 1
    assert result.assignments[0].shift == 'B'
    # A keeps 4 effective hours out of 9; B keeps all 5.
    assert result.by_shift == {'A': 2, 'B': 3}


def test_result_mapping_access_and_section_lookup():
    result = allocate_pulses(6, {'A': (0, 5), 'B': (5, 10)})
    assert len(result) == 2
    assert set(result.keys()) == {'A', 'B'}
    assert result.get('A') == 3
    assert result['B'] == 3
    assert result.section_total('A') == {'A': 3}
    assert result.section_total('A', 'A') == 3
